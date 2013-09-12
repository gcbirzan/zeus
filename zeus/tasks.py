import traceback
import copy
import datetime
import json
import urllib, urllib2
import logging

from functools import wraps
from celery.decorators import task as celery_task

from helios.models import Election, Voter, Poll
from helios.view_utils import render_template_raw

from django.utils.translation import ugettext_lazy as _
from django.utils import translation
from django.core.mail import send_mail, EmailMessage
from django.conf import settings
from django.db import transaction

from zeus.core import from_canonical


logger = logging.getLogger(__name__)


def task(*taskargs, **taskkwargs):
    """
    Task helper to automatically initialize django mechanism using the
    default language set in project settings.
    """
    def wrapper(func):
        @wraps(func)
        def inner(*args, **kwargs):
            prev_language = translation.get_language()
            if prev_language != settings.LANGUAGE_CODE:
                translation.activate(settings.LANGUAGE_CODE)
            ret = func(*args, **kwargs)

        # prevent magic kwargs passthrough
        if not 'accept_magic_kwargs' in taskkwargs:
            taskkwargs['accept_magic_kwargs'] = False
        return celery_task(*taskargs, **taskkwargs)(inner)
    return wrapper


def poll_task(*taskargs, **taskkwargs):
    def wrapper(func):
        #if not 'rate_limit' in taskkwargs:
            #taskkwargs['rate_limit'] = '5/m'
        return task(*taskargs, **taskkwargs)(func)
    return wrapper


@task(rate_limit=getattr(settings, 'ZEUS_VOTER_EMAIL_RATE', '20/m'),
      ignore_result=True)
def single_voter_email(voter_uuid, subject_template, body_template,
                       extra_vars={}, update_date=True,
                       update_booth_invitation_date=False):
    voter = Voter.objects.get(uuid=voter_uuid)
    the_vars = copy.copy(extra_vars)
    the_vars.update({'voter' : voter})
    subject = render_template_raw(None, subject_template, the_vars)
    body = render_template_raw(None, body_template, the_vars)
    if update_date:
        voter.last_email_send_at = datetime.datetime.now()
        voter.save()
    if update_booth_invitation_date:
        voter.last_booth_invitation_send_at = datetime.datetime.now()
        voter.save()
    voter.user.send_message(subject, body)


@task(ignore_result=True)
def voters_email(poll_id, subject_template, body_template, extra_vars={},
                 voter_constraints_include=None,
                 voter_constraints_exclude=None,
                 update_date=True,
                 update_booth_invitation_date=False):
    election = Poll.objects.get(id=poll_id)
    voters = election.voters.all()
    if voter_constraints_include:
        voters = voters.filter(**voter_constraints_include)
    if voter_constraints_exclude:
        voters = voters.exclude(**voter_constraints_exclude)
    for voter in voters:
        single_voter_email.delay(voter.uuid,
                                 subject_template,
                                 body_template,
                                 extra_vars,
                                 update_date,
                                 update_booth_invitation_date)


@task(rate_limit=getattr(settings, 'ZEUS_VOTER_EMAIL_RATE', '20/m'),
      ignore_result=True)
def send_cast_vote_email(poll_pk, voter_pk, signature):
    poll = Poll.objects.get(pk=poll_pk)
    election = poll.election
    voter = poll.voters.filter().get(pk=voter_pk)
    subject = _("%(election_name)s - vote cast") % {
      'election_name': election.name,
      'poll_name': poll.name
    }

    body = _(u"""You have successfully cast a vote in

%(election_name)s
%(poll_name)s

you can find your encrypted vote attached in this mail.
""") % {
    'election_name': election.name,
    'poll_name': poll.name
}

    # send it via the notification system associated with the auth system
    attachments = [('vote.signature', signature['signature'], 'text/plain')]
    to = "%s %s <%s>" % (voter.voter_name, voter.voter_surname,
                         voter.voter_email)
    message = EmailMessage(subject, body, settings.SERVER_EMAIL, [to])
    for attachment in attachments:
        message.attach(*attachment)

    message.send(fail_silently=False)


@poll_task(ignore_result=True)
def poll_validate_create(poll_id):
    poll = Poll.objects.select_for_update().get(id=poll_id)
    poll.validate_create()


@task(ignore_result=True)
def election_validate_create(election_id):
    election = Election.objects.select_for_update().get(id=election_id)
    if election.polls_feature_frozen:
        election.frozen_at = datetime.datetime.now()
        election.save()

    for poll in election.polls.all():
        if not poll.feature_can_validate_create:
            poll_validate_create.delay(poll.id)


@task(ignore_result=True)
def election_validate_voting(election_id):
    election = Election.objects.select_for_update().get(pk=election_id)
    for poll in election.polls.all():
        if poll.feature_can_validate_voting:
            poll_validate_voting.delay(poll.pk)


@poll_task(ignore_result=True)
def poll_validate_voting(poll_id):
    poll = Poll.objects.select_for_update().get(pk=poll_id)
    poll.validate_voting()
    if poll.election.polls_feature_validate_voting_finished:
        election_mix.delay(poll.election.pk)


@task(ignore_result=True)
def election_mix(election_id):
    election = Election.objects.select_for_update().get(pk=election_id)
    for poll in election.polls.all():
        if poll.feature_can_mix:
            poll_mix.delay(poll.pk)


@poll_task(ignore_result=True)
def poll_mix(poll_id):
    poll = Poll.objects.select_for_update().get(pk=poll_id)
    poll.mix()
    if poll.election.polls_feature_mix_finished:
        election_validate_mixing.delay(poll.election.pk)


@task(ignore_result=True)
def election_validate_mixing(election_id):
    election = Election.objects.select_for_update().get(pk=election_id)
    for poll in election.polls.all():
        if poll.feature_can_validate_mixing:
            poll_validate_mixing.delay(poll.pk)


@poll_task(ignore_result=True)
def poll_validate_mixing(poll_id):
    poll = Poll.objects.select_for_update().get(pk=poll_id)
    poll.validate_mixing()
    if poll.election.polls_feature_validate_mixing_finished:
        election_zeus_partial_decrypt.delay(poll.election.pk)


@task(ignore_result=True)
def notify_trustees(election_id):
    election = Election.objects.get(pk=election_id)
    for trustee in election.trustees.filter().no_secret():
        trustee.send_url_via_mail()


@task(ignore_result=True)
def election_zeus_partial_decrypt(election_id):
    election = Election.objects.select_for_update().get(pk=election_id)
    notify_trustees.delay(election.pk)
    for poll in election.polls.all():
        if poll.feature_can_zeus_partial_decrypt:
            poll_zeus_partial_decrypt.delay(poll.pk)


@poll_task(ignore_result=True)
def poll_zeus_partial_decrypt(poll_id):
    poll = Poll.objects.select_for_update().get(pk=poll_id)
    poll.zeus_partial_decrypt()
    if poll.election.polls_feature_partial_decryptions_finished:
        election_decrypt.delay(poll.election.pk)


@poll_task(ignore_result=True)
def poll_add_trustee_factors(poll_id, trustee_id, factors, proofs):
    poll = Poll.objects.select_for_update().get(pk=poll_id)
    trustee = poll.election.trustees.get(pk=trustee_id)
    poll.partial_decrypt(trustee, factors, proofs)
    if poll.election.polls_feature_partial_decryptions_finished:
        election_decrypt.delay(poll.election.pk)


@task(ignore_result=True)
def election_decrypt(election_id):
    election = Election.objects.select_for_update().get(pk=election_id)
    for poll in election.polls.all():
        if poll.feature_can_decrypt:
            poll_decrypt.delay(poll.pk)


@poll_task(ignore_result=True)
def poll_decrypt(poll_id):
    poll = Poll.objects.select_for_update().get(pk=poll_id)
    poll.decrypt()
    if poll.election.polls_feature_decrypt_finished:
        election_compute_results.delay(poll.election.pk)


@task(ignore_result=True)
def election_compute_results(election_id):
    election = Election.objects.select_for_update().get(pk=election_id)
    for poll in election.polls.all():
        if poll.feature_can_compute_results:
            poll_compute_results.delay(poll.pk)


@poll_task(ignore_result=True)
def poll_compute_results(poll_id):
    poll = Poll.objects.select_for_update().get(pk=poll_id)
    poll.compute_results()
    if poll.election.polls_feature_compute_results_finished:
        e = poll.election
        e.completed_at = datetime.datetime.now()
        e.save()
