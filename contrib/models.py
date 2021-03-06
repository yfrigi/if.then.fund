import enum, decimal, copy, json

from django.db import models, transaction, IntegrityError
from django.conf import settings
from django.utils import timezone
from django.dispatch import receiver
from django.contrib.contenttypes.models import ContentType
from enumfields import EnumIntegerField as EnumField

from contrib.bizlogic import get_pledge_recipients, create_pledge_donation, void_pledge_transaction, HumanReadableValidationError

from itfsite.utils import JSONField, TextFormat
from datetime import timedelta

#####################################################################
#
# Utilities / Enums
#
#####################################################################

class ActorParty(enum.Enum):
	Democratic = 1
	Republican = 2
	Independent = 3

	@staticmethod
	def from_letter(letter):
		if letter == "D":
			return ActorParty.Democratic
		if letter == "R":
			return ActorParty.Republican
		raise ValueError(letter)

	def opposite(self):
		if self == ActorParty.Democratic: return ActorParty.Republican
		if self == ActorParty.Republican: return ActorParty.Democratic
		raise ValueError("%s does not have an opposite party." % str(self))


#####################################################################
#
# Triggers
#
# A future event that triggers pledged contributions.
#
#####################################################################

class TriggerType(models.Model):
	"""A class of triggers, like a House vote."""

	key = models.CharField(max_length=64, blank=True, null=True, db_index=True, unique=True, help_text="An opaque look-up key to quickly locate this object.")
	title = models.CharField(max_length=200, help_text="The title for the trigger.")

	created = models.DateTimeField(auto_now_add=True, db_index=True)
	updated = models.DateTimeField(auto_now=True, db_index=True)

	strings = JSONField(default={}, help_text="A dictionary of displayable text.")
	extra = JSONField(blank=True, help_text="Additional information stored with this object.")

	def __str__(self):
		return self.title

class TriggerStatus(enum.Enum):
	Draft = 0
	Open = 1
	Paused = 2
	Executed = 3
	Vacated = 4

class Trigger(models.Model):
	"""A future event that triggers a camapaign contribution, such as a roll call vote in Congress."""

	key = models.CharField(max_length=64, blank=True, null=True, db_index=True, unique=True, help_text="An opaque look-up key to quickly locate this object.")

	title = models.CharField(max_length=200, help_text="The legislative action that this trigger is about, in wonky language.")
	owner = models.ForeignKey('itfsite.Organization', blank=True, null=True, on_delete=models.PROTECT, help_text="The user/organization which created the trigger and can update it. Empty for Triggers created by us.")
	trigger_type = models.ForeignKey(TriggerType, on_delete=models.PROTECT, help_text="The type of the trigger, which determines how it is described in text.")

	created = models.DateTimeField(auto_now_add=True, db_index=True)
	updated = models.DateTimeField(auto_now=True, db_index=True)

	description = models.TextField(help_text="Describe what event will cause contributions to be made. Use the second person and future tense, e.g. by starting with \"Your contribution will...\". The text is in the format given by description_format.")
	description_format = EnumField(TextFormat, default=TextFormat.Markdown, help_text="The format of the description text.")
	status = EnumField(TriggerStatus, default=TriggerStatus.Draft, help_text="The current status of the trigger: Open (accepting pledges), Paused (not accepting pledges), Executed (funds distributed), Vacated (existing pledges invalidated).")
	outcomes = JSONField(
		default=json.dumps([ # so the add form can be sensibly prepopulated --- default is a raw value for some reason
			{ "vote_key": "+", "label": "Yes on This Vote", "object": "in favor of the bill" },
			{ "vote_key": "-", "label": "No on This Vote", "object": "against passage of the bill" },
		]),
		help_text="An array (order matters!) of information for each possible outcome of the trigger, e.g. ['Voted Yes', 'Voted No'].")

	extra = JSONField(blank=True, help_text="Additional information stored with this object.")

	pledge_count = models.IntegerField(default=0, help_text="A cached count of the number of pledges made *prior* to trigger execution (excludes Pledges with made_after_trigger_execution).")
	total_pledged = models.DecimalField(max_digits=6, decimal_places=2, default=0, db_index=True, help_text="A cached total amount of pledges made *prior* to trigger execution (excludes Pledges with made_after_trigger_execution).")

	def __str__(self):
		return "Trigger(%d, %s)" % (self.id, self.title[0:30])

	@property
	def verb(self):
		if self.status != TriggerStatus.Executed:
			# If the trigger has not yet been executed, then use the future tense.
			return self.trigger_type.strings['action_vb_inf']
		else:
			# If the trigger has been executed, then use the past tense.
			return self.trigger_type.strings['action_vb_past']

	@property
	def verb_pres_s(self):
		if self.status != TriggerStatus.Executed:
			# If the trigger has not yet been executed, then use the future tense.
			return self.trigger_type.strings['action_vb_pres_s']
		else:
			# If the trigger has been executed, then use the past tense.
			return self.trigger_type.strings['action_vb_past']

	def outcome_strings(self):
		# "overridden" by TriggerCustomizations
		return self.outcomes

	def get_minimum_pledge(self):
		# What's the minimum pledge size for this trigger?
		# It's at least Pledge.current_algorithm.min_contrib
		# and at least the amount we need to do one cent per
		# possible recipient, plus fees.
		alg = Pledge.current_algorithm()
		m1 = alg['min_contrib']
		m2 = 0
		max_split = self.max_split()
		if True:
			# The minimum pledge is one cent to all possible recipients, plus fees.
			m2 = decimal.Decimal('0.01') * max_split * (1 + alg['fees_percent']) + alg['fees_fixed']
			m2 = m2.quantize(decimal.Decimal('.01'), rounding=decimal.ROUND_UP)
		return max(m1, m2)

	def get_suggested_pledge(self):
		# What's a nice round number to suggest the user pledge?
		# It's the smallest of these pre-set numbers that's greater
		# than the minimum.
		# NOTE: Don't offer anything larger than Pledge.current_algorithm.max_contrib!
		m = self.get_minimum_pledge()
		for amt_str in ('2.50', '4', '5', '10', '15'):
			amt = decimal.Decimal(amt_str)
			if amt >= m:
				return amt
		# None of our nice rounded amounts are greater, so just offer
		# the minimum. This should never really happen.
		return m

	def max_split(self):
		if self.status != TriggerStatus.Executed:
			# If the Trigger isn't executed yet, we don't know how
			# many recipients there will be.
			return self.trigger_type.extra['max_split']
		else:
			# The Trigger is executed and so we know exactly how many
			# recipients there could be if the user does not apply
			# any filters.
			if self.extra and "subtriggers" in self.extra:
				# This is a super-trigger. Add together the max_splits
				# of the subtriggers.
				return sum(
					Trigger.objects.get(id=rec["trigger"]).max_split()
					for rec in self.extra["subtriggers"])

			else:
				# This is a regular Trigger. Just look at its executed Actions.
				return self.execution.actions.exclude(outcome=None).count()

	# Execute.
	@transaction.atomic
	def execute(self, action_time, actor_outcomes, description, description_format, extra):
		# Executes the trigger.

		# Lock the trigger to prevent race conditions and make sure the Trigger
		# is either Open or Paused.
		trigger = Trigger.objects.select_for_update().filter(id=self.id).first()
		if trigger.status not in (TriggerStatus.Draft, TriggerStatus.Open, TriggerStatus.Paused):
			raise ValueError("Trigger is in state %s." % str(trigger.status))

		# Create TriggerExecution object.
		te = TriggerExecution()
		te.trigger = trigger
		te.cycle = settings.CURRENT_ELECTION_CYCLE
		te.action_time = action_time
		te.description = description
		te.description_format = description_format
		te.extra = extra
		te.save()

		# Create Action objects which represent what each Actor did.
		# actor_outcomes is a dict mapping Actors to outcome indexes
		# or None if the Actor didn't properly participate or a string
		# meaning the Actor didn't participate and the string gives
		# the reason_for_no_outcome value.
		for actor_outcome in actor_outcomes:
			# If an Actor has an inactive_reason set, then we ignore
			# any outcome supplied to us and replace it with that.
			# Probably 'Not running for reelection.'.
			if actor_outcome["actor"].inactive_reason:
				actor_outcome["outcome"] = actor_outcome["actor"].inactive_reason

			ac = Action.create(te, actor_outcome["actor"], actor_outcome["outcome"], actor_outcome.get("action_time"))

		# Mark as executed.
		trigger.status = TriggerStatus.Executed
		trigger.save()

	# Vacate, meaning we do not expect the action to ever occur.
	@transaction.atomic
	def vacate(self):
		trigger = Trigger.objects.select_for_update().filter(id=self.id).first()
		if trigger.status not in (TriggerStatus.Open, TriggerStatus.Paused):
			raise ValueError("Trigger is in state %s." % str(trigger.status))

		# Mark as vacated.
		trigger.status = TriggerStatus.Vacated
		trigger.save()

		# Mark all pledges as vacated.
		pledges = trigger.pledges.select_for_update()
		for p in pledges:
			if p.status != PledgeStatus.Open:
				raise ValueError("Pledge %s is in state %s." % (repr(p), str(p.status)))
			p.status = PledgeStatus.Vacated
			p.save()

	def clone_as_announced_positions_on(self):
		t = Trigger()
		t.status = TriggerStatus.Open # so we can execute it
		t.key = self.key + ":announced"
		t.title = "Announced Positions on " + self.title
		t.owner = self.owner
		t.trigger_type = TriggerType.objects.get_or_create(
			key = "announced-positions",
			defaults = {
				"strings": {
					"actor": 'member of Congress',
					"actors": 'members of Congress',
					"action_vb_inf": "announce they would vote",
					"action_vb_pres_s": "announces they would vote",
					"action_vb_past": "announced they would vote",
			}})[0]
		t.description = "n/a"
		t.description_format = TextFormat.HTML
		t.outcomes = self.outcomes
		t.extra = self.extra
		t.save()

		t.execute_empty()

		return t

	def execute_empty(self):
		# Execute with no actor information.
		if self.status == TriggerStatus.Draft:
			self.status = TriggerStatus.Open # so we can execute it
			self.save()
		self.execute(self.created, [], "Empty.", TextFormat.HTML, { })

class TriggerStatusUpdate(models.Model):
	"""A status update about the Trigger providing further information to users looking at the Trigger that was not known when the Trigger was created."""

	trigger = models.ForeignKey(Trigger, on_delete=models.CASCADE, help_text="The Trigger that this update is about.")
	created = models.DateTimeField(auto_now_add=True, db_index=True)
	updated = models.DateTimeField(auto_now=True)
	text = models.TextField(help_text="Status update text in the format given by text_format.")
	text_format = EnumField(TextFormat, help_text="The format of the text.")

class TriggerRecommendation(models.Model):
	trigger1 = models.ForeignKey(Trigger, related_name="recommends", on_delete=models.CASCADE, help_text="If a user has taken action on this Trigger, then we send them a notification.")
	trigger2 = models.ForeignKey(Trigger, related_name="recommended_by", on_delete=models.CASCADE, help_text="This is the trigger that we recommend the user take action on.")
	symmetric = models.BooleanField(default=False, help_text="If true, the recommendation goes both ways.")
	created = models.DateTimeField(auto_now_add=True, db_index=True)
	notifications_created = models.BooleanField(default=False, db_index=True, help_text="Set to true once notifications have been generated for users for any past actions the users took before this recommendation was added.")

	def __str__(self):
		return " ".join([
			str(self.trigger1),
			"=>" if not self.symmetric else "<=>",
			str(self.trigger2),
		])

	def save(self, *args, override_immutable_check=False, **kwargs):
		# Prevent the instance from being modified. Then save.
		if not override_immutable_check and self.id: raise Exception("This model is immutable.")
		super(TriggerRecommendation, self).save(*args, **kwargs)


class TriggerCustomization(models.Model):
	"""The specialization of a trigger for an Organization."""

	owner = models.ForeignKey('itfsite.Organization', related_name="triggers", on_delete=models.CASCADE, help_text="The user/organization which created the TriggerCustomization.")
	trigger = models.ForeignKey(Trigger, related_name="customizations", on_delete=models.CASCADE, help_text="The Trigger that this TriggerCustomization customizes.")

	created = models.DateTimeField(auto_now_add=True, db_index=True)
	updated = models.DateTimeField(auto_now=True, db_index=True)

	outcome = models.IntegerField(blank=True, null=True, verbose_name="Restrict Outcome", help_text="Restrict Pledges to this outcome.")
	incumb_challgr = models.FloatField(blank=True, null=True, verbose_name="Restrict Incumbent-Challenger Choice", help_text="Restrict Pledges to be for just incumbents, just challengers, both incumbents and challengers (where user can't pick), or don't restrict the user's choice.")
	filter_party = EnumField(ActorParty, blank=True, null=True, verbose_name="Restrict Party", help_text="Restrict Pledges to be to candidates of this party.")
	filter_competitive = models.NullBooleanField(default=False, verbose_name="Restrict Competitive Filter", help_text="Restrict Pledges to this filter_competitive value.")

	extra = JSONField(blank=True, help_text="Additional information stored with this object.")

	class Meta:
		unique_together = ('trigger', 'owner')

	def __str__(self):
		return "%s / %s" % (self.owner, self.trigger)

	def has_fixed_outcome(self):
		return self.outcome is not None

	def outcome_strings(self):
		if self.extra and self.extra.get('outcome_strings'):
			# Merge keys from the outcome_strings here and the outcome strings of the trigger.
			def merge_strings(a, b):
				r = dict(a)
				r.update(b)
				return r
			return [merge_strings(*pair) for pair in zip(self.trigger.outcome_strings(), self.extra['outcome_strings'])]
		else:
			# Use the outcome strings from the Trigger. No customization here.
			return self.trigger.outcome_strings()

	def get_outcome(self):
		if self.outcome is None: raise ValueError()
		return self.outcome_strings()[self.outcome]

class TriggerExecution(models.Model):
	"""How a Trigger was executed."""

	trigger = models.OneToOneField(Trigger, related_name='execution', on_delete=models.PROTECT, help_text="The Trigger this execution information is about.")

	created = models.DateTimeField(auto_now_add=True, db_index=True)
	updated = models.DateTimeField(auto_now=True, db_index=True)
	action_time = models.DateTimeField(help_text="The date & time the action actually ocurred in the real world.")

	cycle = models.IntegerField(help_text="The election cycle (year) that the trigger was executed in.")

	description = models.TextField(help_text="Describe how contriutions are being distributed. Use the passive voice and present progressive tense, e.g. by starting with \"Contributions are being distributed...\".")
	description_format = EnumField(TextFormat, help_text="The format of the description text.")

	# The pledge_count and pledge_count_with_contribs fields count up the total
	# number of Pledges (& executed with at least one contribution) where the
	# Pledge's trigger's execution is this instance. cf. the next fields.
	pledge_count = models.IntegerField(default=0, help_text="A cached count of the number of pledges executed. This counts pledges from anonymous users that do not result in contributions. Used to check when a Trigger is done executing.")
	pledge_count_with_contribs = models.IntegerField(default=0, help_text="A cached count of the number of pledges executed with actual contributions made.")

	# The num_contributions and total_contributions fields count up the total
	# contributions for this TriggerExeuction. When a Pledge executes over multiple
	# Triggers, then the contributions are double-counted: once in the Pledge's trigger's
	# exeuction and ones in the Action's TriggerExecution. So don't aggregate these
	# fields across Triggers!
	num_contributions = models.IntegerField(default=0, db_index=True, help_text="A cached total number of campaign contributions executed.")
	total_contributions = models.DecimalField(max_digits=6, decimal_places=2, default=0, db_index=True, help_text="A cached total amount of campaign contributions executed, excluding fees.")

	extra = JSONField(blank=True, help_text="Additional information stored with this object.")

	def __str__(self):
		return "%s [exec %s]" % (self.trigger, self.created.strftime("%x"))

	def delete(self, *args, **kwargs):
		# After deleting a TriggerExecution, reset the status of the trigger
		# to Paused. Leaving it as Executed would leave it in an inconsistent
		# state and makes debugging harder.
		super(TriggerExecution, self).delete(*args, **kwargs)
		self.trigger.status = TriggerStatus.Paused
		self.trigger.save(update_fields=['status'])

	def most_recent_pledge_execution(self):
		return self.pledges.order_by('-created').first()

	def get_sorted_actions(self):
		ret = list(self.actions.order_by('outcome', 'name_sort'))
		ret.sort(key = lambda x : (x.outcome is None, x.outcome, x.reason_for_no_outcome))
		return ret


	def get_outcome_summary(self):
		counts = list(self.actions.values("outcome", "reason_for_no_outcome").annotate(count=models.Count('id')))
		counts.sort(key = lambda x : (x["outcome"] is None, x["outcome"], x["reason_for_no_outcome"]))
		for count in counts:
			if count['outcome'] is None:
				count['label'] = count['reason_for_no_outcome']
			else:
				count.update(self.trigger.outcome_strings()[count['outcome']])
		return counts


#####################################################################
#
# Actors
#
# Elected officials and their official acts.
#
#####################################################################

class Actor(models.Model):
	"""A public figure, e.g. elected official with an election campaign, who might take an action."""

	govtrack_id = models.IntegerField(unique=True, help_text="GovTrack's ID for this person.")
	votervoice_id = models.IntegerField(blank=True, null=True, unique=True, help_text="VoterVoice's target ID for this person.")

	office = models.CharField(max_length=7, blank=True, null=True, unique=True, help_text="A code specifying the office currently held by the Actor, in the same format as Recipient.office_sought.")

	name_long = models.CharField(max_length=128, help_text="The long form of the person's current name, meant for a page title.")
	name_short = models.CharField(max_length=128, help_text="The short form of the person's current name, usually a last name, meant for in-page second references.")
	name_sort = models.CharField(max_length=128, help_text="The sorted list form of the person's current name.")
	party = EnumField(ActorParty, help_text="The current party of the Actor. For Members of Congress, this is based on how the Member caucuses to avoid Independent as much as possible.")
	title = models.CharField(max_length=200, help_text="Descriptive text for the office held by this actor.")
	
	extra = JSONField(blank=True, help_text="Additional information stored with this object.")

	challenger = models.OneToOneField('Recipient', unique=True, null=True, blank=True, related_name="challenger_to", help_text="The *current* Recipient that contributions to this Actor's challenger go to. Independents don't have challengers because they have no opposing party.")
	inactive_reason = models.CharField(blank=True, null=True, max_length=200, help_text="If the Actor is still a public official (i.e. generates Actions) but should not get contributions, the reason why. If not None, serves as a flag. E.g. 'Not running for reelection.'.")

	def __str__(self):
		return self.name_sort

class Action(models.Model):
	"""The outcome of an actor taking an act described by a trigger."""

	execution = models.ForeignKey(TriggerExecution, related_name="actions", on_delete=models.CASCADE, help_text="The TriggerExecution that created this object.")
	action_time = models.DateTimeField(db_index=True, help_text="The date & time the action actually ocurred in the real world.")
	actor = models.ForeignKey(Actor, on_delete=models.PROTECT, help_text="The Actor who took this action.")
	outcome = models.IntegerField(blank=True, null=True, help_text="The outcome index that was taken. May be null if the Actor should have participated but didn't (we want to record to avoid counterintuitive missing data).")

	name_long = models.CharField(max_length=128, help_text="The long form of the person's name at the time of the action, meant for a page title.")
	name_short = models.CharField(max_length=128, help_text="The short form of the person's name at the time of the action, usually a last name, meant for in-page second references.")
	name_sort = models.CharField(max_length=128, help_text="The sorted list form of the person's name at the time of the action.")
	party = EnumField(ActorParty, help_text="The party of the Actor at the time of the action.")
	title = models.CharField(max_length=200, help_text="Descriptive text for the office held by this actor at the time of the action.")
	office = models.CharField(max_length=7, blank=True, null=True, help_text="A code specifying the office held by the Actor at the time the Action was created, in the same format as Recipient.office_sought.")
	extra = JSONField(blank=True, help_text="Additional information stored with this object.")

	challenger = models.ForeignKey('Recipient', null=True, blank=True, help_text="The Recipient that contributions to this Actor's challenger go to, at the time of the Action. Independents don't have challengers because they have no opposing party.")

	total_contributions_for = models.DecimalField(max_digits=6, decimal_places=2, default=0, help_text="A cached total amount of campaign contributions executed with the actor as the recipient (excluding fees).")
	total_contributions_against = models.DecimalField(max_digits=6, decimal_places=2, default=0, help_text="A cached total amount of campaign contributions executed with an opponent of the actor as the recipient (excluding fees).")

	reason_for_no_outcome = models.CharField(blank=True, null=True, max_length=200, help_text="If outcome is null, why. E.g. 'Did not vote.'.")

	class Meta:
		unique_together = [('execution', 'actor')]

	def __str__(self):
		return "%s is %s | %s" % (
			self.actor,
			self.outcome_label(),
			self.execution)

	def has_outcome(self):
		return self.outcome is not None

	def outcome_label(self):
		if self.outcome is not None:
			return self.execution.trigger.outcomes[self.outcome]['label']
		if self.reason_for_no_outcome:
			return self.reason_for_no_outcome
		return "N/A"

	@staticmethod
	def create(execution, actor, outcome, action_time):
		# outcome can be an integer giving the Trigger's outcome index
		# that the Actor did . . .
		if isinstance(outcome, int):
			outcome_index = outcome
			reason_for_no_outcome = None

		# Or it can be None or a string giving an explanation for why
		# the Action has no outcome.
		else:
			outcome_index = None
			reason_for_no_outcome = outcome

		# Create the Action instance.
		a = Action()
		a.execution = execution
		a.actor = actor
		a.outcome = outcome_index
		a.action_time = action_time or execution.action_time
		a.reason_for_no_outcome = reason_for_no_outcome

		# Copy fields that may change on the Actor but that we want to know what they were
		# at the time this Action ocurred.
		for f in ('name_long', 'name_short', 'name_sort', 'party', 'title', 'office', 'extra', 'challenger'):
			setattr(a, f, getattr(actor, f))

		# Save.
		a.save()
		return a


#####################################################################
#
# Pledges
#
# A pledged campaign contribution by a user.
#
#####################################################################

class ContributorInfo(models.Model):
	"""Contributor and billing information used for a Pledge. Stored schema-less in the extra field. May be shared across Pledges of the same user. Instances are immutable."""

	created = models.DateTimeField(auto_now_add=True, db_index=True)

	cclastfour = models.CharField(max_length=4, blank=True, null=True, db_index=True, help_text="The last four digits of the user's credit card number, stored & indexed for fast look-up in case we need to find a pledge from a credit card number.")
	is_geocoded = models.BooleanField(default=False, db_index=True, help_text="Whether this record has been geocoded.")

	extra = JSONField(blank=True, help_text="Schemaless data stored with this object.")

	def __str__(self):
		return "[%d] %s %s" % (self.id, self.name, self.address)

	def save(self, *args, override_immutable_check=False, **kwargs):
		if self.id and not override_immutable_check:
			raise Exception("This model is immutable.")
		super(ContributorInfo, self).save(*args, **kwargs)

	def can_delete(self):
		return not self.pledges.exists() and not self.tips.exists()

	@property
	def name(self):
		return ' '.join(self.extra['contributor'][k] for k in ('contribNameFirst', 'contribNameLast'))

	@property
	def address(self):
		return ', '.join(self.extra['contributor'][k] for k in ('contribCity', 'contribState'))

	def set_from(self, data):
		# Initialize from a dictionary.

		# Store the last four digits of the credit card number so we can
		# quickly locate a Pledge by CC number (approximately).
		self.cclastfour = data['billing']['cc_num'][-4:]

		# Store a hashed version of the credit card number so we can
		# do a verification if the user wants to look up a Pledge by CC
		# info. Use Django's built-in password hashing functionality to
		# handle this. Then clear the cc_num field.
		from django.contrib.auth.hashers import make_password
		data['billing']['cc_num_hashed'] = make_password(data['billing']['cc_num'])
		del data['billing']['cc_num']

		# Store the rest in extra.
		self.extra = data

	def same_as(self, other):
		import json
		def normalize(data): return json.dumps(data, sort_keys=True)
		return (self.cclastfour == other.cclastfour) and (normalize(self.extra) == normalize(other.extra))

	def open_pledge_count(self):
		return self.pledges.filter(status=PledgeStatus.Open).count()

	def geocode(self):
		# Updates this record with geocoder information, especially congressional district
		# and timezone.
		from contrib.legislative import geocode
		info = geocode([
			self.extra['contributor']['contribAddress'],
			self.extra['contributor']['contribCity'],
			self.extra['contributor']['contribState'],
			self.extra['contributor']['contribZip']])
		self.extra['geocode'] = info
		self.is_geocoded = True
		self.save(update_fields=['is_geocoded', 'extra'], override_immutable_check=True)

	@staticmethod
	def find_from_cc(cc_number):
		# Returns an interator that yields matchinig Pledge instances.
		# Must be in parallel to how the view function creates the pledge.
		from django.contrib.auth.hashers import check_password
		cc_number = cc_number.replace(' ', '')
		for p in ContributorInfo.objects.filter(cclastfour=cc_number[-4:]):
			if check_password(cc_number, p.extra['billing']['cc_num_hashed']):
				yield p

	@staticmethod
	def createRandom():
		# For testing!
		import random
		return ContributorInfo.objects.create(extra={
			"contributor": {
				"contribNameFirst": random.choice(["Jeanie", "Lucrecia", "Marvin", "Jasper", "Carlo", "Millicent", "Zack", "Raul", "Johnny", "Margarette"]),
				"contribNameLast": random.choice(["Ramm", "Berns", "Wannamaker", "McCarroll", "Bumbrey", "Caudle", "Bridwell", "Pacelli", "Crowley", "Montejano"]),
				"contribAddress": "%d %s %s" % (random.randint(10, 200), random.choice(["Fir", "Maple", "Cedar", "Dogwood", "Persimmon", "Beech"]), random.choice([ "St", "Ave", "Ct"])),
				"contribCity": random.choice(["Rudy", "Hookerton", "La Ward", "Marenisco", "Nara Visa"]),
				"contribState": random.choice(["NQ", "BL", "PS"]),
				"contribZip": random.randint(10000, 88888),
				"contribEmployer": "self",
				"contribOccupation": "unspecified",
			},
			"billing": {
				"de_cc_token": "_made_up_%d" % random.randint(1, 100000),
			},
		})

class PledgeStatus(enum.Enum):
	Open = 1
	Executed = 2
	Vacated = 10 # trigger was vacated, pledge is considered vacated

class NoMassDeleteManager(models.Manager):
	class CustomQuerySet(models.QuerySet):
		def delete(self, *args, **kwargs):
			# Can't do a mass delete because it would not update Trigger.total_pledged,
			# in the case of the Pledge model.
			#
			# Instead call delete() on each instance, which handles the constraint.
			for obj in self:
				obj.delete(*args, **kwargs)
	def get_queryset(self):
		return NoMassDeleteManager.CustomQuerySet(self.model, using=self._db)

class Pledge(models.Model):
	"""A user's pledge of a contribution."""

	user = models.ForeignKey('itfsite.User', blank=True, null=True, on_delete=models.PROTECT, help_text="The user making the pledge. When an anonymous user makes a pledge, this is null, the user's email address is stored in an AnonymousUser object referenced in anon_user, and the pledge should be considered unconfirmed/provisional and will not be executed.")
	anon_user = models.ForeignKey('itfsite.AnonymousUser', blank=True, null=True, on_delete=models.CASCADE, help_text="When an anonymous user makes a pledge, a one-off object is stored here and we send a confirmation email.")
	profile = models.ForeignKey(ContributorInfo, related_name="pledges", on_delete=models.PROTECT, help_text="The contributor information (name, address, etc.) and billing information used for this Pledge. Immutable and cannot be changed after execution.")

	trigger = models.ForeignKey(Trigger, related_name="pledges", on_delete=models.PROTECT, help_text="The Trigger that this Pledge is for.")
	via_campaign = models.ForeignKey('itfsite.Campaign', blank=True, null=True, related_name="pledges", on_delete=models.PROTECT, help_text="The Campaign that this Pledge was made via.")

	ref_code = models.CharField(max_length=24, blank=True, null=True, db_index=True, help_text="An optional referral code that lead the user to take this action.")

	# When a Pledge is cancelled, the object is deleted. The trigger/via_campaign/user/anon_user fields
	# are archived, plus the fields listed in this list. The fields below must
	# be JSON-serializable.
	cancel_archive_fields = (
		'created', 'updated', 'ref_code',
		'algorithm', 'desired_outcome', 'amount',
		)

	created = models.DateTimeField(auto_now_add=True, db_index=True)
	updated = models.DateTimeField(auto_now=True)
	algorithm = models.IntegerField(default=0, help_text="In case we change our terms & conditions, or our explanation of how things work, an integer indicating the terms and expectations at the time the user made the pledge.")
	status = EnumField(PledgeStatus, default=PledgeStatus.Open, help_text="The current status of the pledge.")
	made_after_trigger_execution = models.BooleanField(default=False, help_text="Whether this Pledge was created after the Trigger was executed (i.e. outcomes known).")

	desired_outcome = models.IntegerField(help_text="The outcome index that the user desires.")
	amount = models.DecimalField(max_digits=6, decimal_places=2, help_text="The pledge amount in dollars (including fees). The credit card charge may be less in the event that we have to round to the nearest penny-donation.")
	incumb_challgr = models.FloatField(help_text="A float indicating how to split the pledge: -1 (to challenger only) <=> 0 (evenly split between incumbends and challengers) <=> +1 (to incumbents only)")
	filter_party = EnumField(ActorParty, blank=True, null=True, help_text="Contributions only go to candidates whose party matches this party. Independent is not an allowed value here.")
	filter_competitive = models.BooleanField(default=False, help_text="Whether to filter contributions to competitive races.")

	tip_to_campaign_owner = models.DecimalField(max_digits=6, decimal_places=2, default=decimal.Decimal(0), help_text="The amount in dollars that the user desires to send to the owner of via_campaign, zero if there is no one to tip or the user desires not to tip.")

	cclastfour = models.CharField(max_length=4, blank=True, null=True, db_index=True, help_text="The last four digits of the user's credit card number, stored & indexed for fast look-up in case we need to find a pledge from a credit card number.")

	email_confirmed_at = models.DateTimeField(blank=True, null=True, help_text="The date and time that the email address of the pledge became confirmed, if the pledge was originally based on an unconfirmed email address.")
	pre_execution_email_sent_at = models.DateTimeField(blank=True, null=True, help_text="The date and time when the user was sent an email letting them know that their pledge is about to be executed.")
	post_execution_email_sent_at = models.DateTimeField(blank=True, null=True, help_text="The date and time when the user was sent an email letting them know that their pledge was executed.")

	extra = JSONField(blank=True, help_text="Additional information stored with this object.")

	class Meta:
		unique_together = [('trigger', 'user'), ('trigger', 'anon_user')]
		index_together = [('trigger', 'via_campaign')]

	objects = NoMassDeleteManager()

	ENFORCE_EXECUTION_EMAIL_DELAY = True # can disable for testing

	@transaction.atomic
	def save(self, *args, **kwargs):
		# Override .save() so on the INSERT of a new Pledge we increment
		# counters on the Trigger.
		is_new = (not self.id) # if the pk evaluates to false, Django does an INSERT

		# Actually save().
		super(Pledge, self).save(*args, **kwargs)

		# For a new object, increment the trigger's pledge_count and total_pledged
		# fields (atomically) if this Pledge was made prior to trigger execution.
		if is_new and not self.made_after_trigger_execution:
			from django.db import models
			self.trigger.pledge_count = models.F('pledge_count') + 1
			self.trigger.total_pledged = models.F('total_pledged') + self.amount
			self.trigger.save(update_fields=['pledge_count', 'total_pledged'])

	@transaction.atomic
	def delete(self):
		if self.status != PledgeStatus.Open:
			raise ValueError("Cannot cancel a Pledge with status %s." % self.status)

		# Decrement the Trigger's pledge_count and total_pledged if the Pledge
		# was made prior to trigger execution.
		if not self.made_after_trigger_execution:
			self.trigger.pledge_count = models.F('pledge_count') - 1
			self.trigger.total_pledged = models.F('total_pledged') - self.amount
			self.trigger.save(update_fields=['pledge_count', 'total_pledged'])

		# Archive as a cancelled pledge.
		cp = CancelledPledge.from_pledge(self)

		# Remove record. Will raise an exception and abort the transaction if
		# the pledge has been executed and a PledgeExecution object refers to this.
		super(Pledge, self).delete()	

	def get_absolute_url(self):
		return self.via_campaign.get_absolute_url()

	@staticmethod
	def current_algorithm():
		return {
			"id": 1, # a sequence number so we can track changes to our fee structure, etc.
			"min_contrib": 1, # dollars
			"max_contrib": 500, # dollars
			"fees_fixed": decimal.Decimal("0.20"), # 20 cents, convert from string so it is exact
			"fees_percent": decimal.Decimal("0.09"), # 0.09 means 9%, convert from string so it is exact
			"pre_execution_warn_time": (timedelta(days=1), "this time tomorrow"),
		}

	def __str__(self):
		return self.get_email() + " => " + str(self.trigger)

	def get_email(self):
		if self.user:
			return self.user.email
		else:
			return self.anon_user.email

	@property
	def get_nice_status(self):
		if self.status != PledgeStatus.Executed:
			return self.status.name
		elif self.execution.problem == PledgeExecutionProblem.NoProblem:
			return "Finished"
		else:
			return "Failed"

	@property
	def is_executed(self):
		return self.status == PledgeStatus.Executed

	@property
	def targets_summary(self):
		# This is mirrored in pledge_form.html.

		def outcome_label(outcome):
			x = self.trigger.outcomes[outcome]
			return x.get("object", x["label"])
		desired_outcome_label = outcome_label(self.desired_outcome)
		if len(self.trigger.outcomes) != 2:
			raise ValueError("Trigger has more than two options.")
		antidesired_outcome_label = outcome_label(1 - self.desired_outcome)

		party_filter = ""
		anti_party_filter = ""
		if self.filter_party is not None:
			party_filter = self.filter_party.name + " "
			anti_party_filter = self.filter_party.opposite().name + " "

		noun = self.trigger.trigger_type.strings['actors']
		verb = self.trigger.verb
		is_monovalent = (self.trigger.trigger_type.extra or {}).get("monovalent")

		if self.incumb_challgr == 1 or (is_monovalent and self.desired_outcome == 0):
			# "keep em in"
			return "%s%s who %s %s" \
				% (party_filter, noun, verb, desired_outcome_label)
		elif self.incumb_challgr == -1 or (is_monovalent and self.desired_outcome > 0):
			# "throw em out"
			return "the %sopponents in the next general election of %s%s who %s %s" \
				% (party_filter, anti_party_filter, noun, verb, antidesired_outcome_label)
		elif party_filter == "":
			# goes to incumbents and challengers, no party filter
			if self.status != PledgeStatus.Executed:
				count = "up to %d" % self.trigger.max_split()
			else:
				count = str(self.execution.contributions.count())
			return "%s %s, each getting a part of your contribution if they %s %s, but if they %s %s their part of your contribution will go to their next general election opponent" \
				% (count, noun, verb, desired_outcome_label,
				   verb, antidesired_outcome_label)
		else:
			# goes to incumbents and challengers, with a party filter
			return "%s%s who %s %s and the %sopponents in the next general election of %s%s who %s %s" \
				% (party_filter, noun, verb, desired_outcome_label,
				                 party_filter, anti_party_filter, noun, verb, antidesired_outcome_label)

	@property
	def is_from_long_ago(self):
		return timezone.now() - self.created > timedelta(days=21)

	def set_confirmed_user(self, user, request):
		# The user may have anonymously created a second Pledge for the same
		# trigger. We can't tell them before they confirm their email that
		# they already made a pledge. We can't confirm both --- warn the user
		# and go on.

		from django.contrib import messages

		if self.trigger.pledges.filter(user=user).exists():
			messages.add_message(request, messages.ERROR, 'You had a previous contribution already scheduled for the same thing. Your more recent contribution will be ignored.')
			self.delete() # else we will try to confirm the email address indefinitely, but the AnonymousUser for this is already confirmed, so it would be an error
			return

		# Move this anonymous pledge to the user's account.
		self.user = user
		self.anon_user = None
		self.email_confirmed_at = timezone.now()
		self.save(update_fields=['user', 'anon_user', 'email_confirmed_at'])

		# Let the user know what happened.
		messages.add_message(request, messages.SUCCESS, 'Your contribution regarding %s has been confirmed.'
			% self.trigger.title)

	def needs_pre_execution_email(self):
		# If the user confirmed their email address after the trigger
		# was executed, then the pre-execution emails already went out
		# and this user probably did not get one because those are only
		# sent if the email address is confirmed. We don't want to cause
		# a delay for everyone else, so these users just don't get a
		# confirmation.
		trigger_execution = self.trigger.execution
		if self.email_confirmed_at and self.email_confirmed_at >= trigger_execution.created:
			return False

		# If the pledge itself was created after the trigger was executed,
		# then we don't send the pre-execution email so we can execute as
		# quickly as possible.
		if self.made_after_trigger_execution:
			return False

		return True

	def can_execute(self):
		# Returns whether a Pledge can be executed.

		# Check Pledge and Trigger state.
		if self.status != PledgeStatus.Open:
			return False
		if self.trigger.status != TriggerStatus.Executed:
			return False
		if self.algorithm != Pledge.current_algorithm()['id']:
			return False

		# Check that a pre-execution email has been sent, if necessary.
		if self.pre_execution_email_sent_at is None:
			if self.needs_pre_execution_email():
				# Not all pledges require the pre-exeuction email (see that function
				# for details, but it's users who confirm their email address too late).
				return False
		
		# The pre-execution email is sent, and now we give the user time to cancel
		# their pledge prior to executing it.
		elif (timezone.now() - self.pre_execution_email_sent_at) < Pledge.current_algorithm()['pre_execution_warn_time'][0] \
				and not settings.DEBUG and Pledge.ENFORCE_EXECUTION_EMAIL_DELAY:
			return False

		return True

	@transaction.atomic # needed b/c of select_for_update
	def execute(self):
		# Lock the Pledge and the Trigger to prevent race conditions.
		pledge = Pledge.objects.select_for_update().filter(id=self.id).first()
		pledge.trigger = Trigger.objects.select_for_update().filter(id=pledge.trigger.id).first()
		trigger_execution = pledge.trigger.execution

		# Validate state.
		if not pledge.can_execute():
			raise ValueError("Pledge cannot be executed.")

		# Default values.
		problem = PledgeExecutionProblem.NoProblem
		exception = None
		recip_contribs = []
		fees = 0
		total_charge = 0
		de_don = None

		# Get the intended recipients of the pledge, as a list of tuples of
		# (Recipient, Action). The pledge filters may result in there being
		# no actual recipients.
		recipients = get_pledge_recipients(pledge)

		if len(recipients) == 0:
			# If there are no matching recipients, we don't make a credit card chage.
			problem = PledgeExecutionProblem.FiltersExcludedAll

		else:
			# Make the donation (an authorization, since Democracy Engine does a capture later).
			#
			# (The transaction records created by the donation are not immediately
			# available, so we know success but can't get further details.)
			try:
				recip_contribs, fees, total_charge, de_don = \
					create_pledge_donation(pledge, recipients)

			# Catch typical exceptions and log them in the PledgeExecutionObject.
			except HumanReadableValidationError as e:
				problem = PledgeExecutionProblem.TransactionFailed
				exception = str(e)

		# From here on, if there is a problem, then the transaction will have gone
		# through but we won't have a record of it.
		try:
			# Create PledgeExecution object.
			pe = PledgeExecution()
			pe.pledge = pledge
			pe.trigger_execution = trigger_execution
			pe.problem = problem
			pe.charged = total_charge
			pe.fees = fees
			pe.extra = {
				"donation": de_don, # donation record, which refers to transactions
				"exception": exception, 
			}
			pe.save()

			# Create Contribution objects.
			for action, recipient_type, recipient, amount in recip_contribs:
				c = Contribution()
				c.pledge_execution = pe
				c.action = action
				c.recipient_type = recipient_type
				c.recipient = recipient
				c.amount = amount
				c.de_id = recipient.de_id
				c.save()

				# Increment the TriggerExecution and Action's total_contributions.
				c.update_aggregates()

			# Mark pledge as executed.
			pledge.status = PledgeStatus.Executed
			pledge.save()

			# Increment TriggerExecution's pledge_count so that we know how many pledges
			# have been or have not yet been executed.
			trigger_execution.pledge_count = models.F('pledge_count') + 1
			if len(recip_contribs) > 0:
				trigger_execution.pledge_count_with_contribs = models.F('pledge_count_with_contribs') + 1
			trigger_execution.save(update_fields=['pledge_count', 'pledge_count_with_contribs'])

		except Exception as e:
			# If a DE transaction was made, include its info in any exception that was raised.
			if de_don:
				try:
					import rtyaml
					x = rtyaml.dump(de_don)
				except:
					x = repr(de_don)
				raise Exception("Something went wrong saving a pledge execution to the database (%s) but the DemocracyEngine transaction was already submitted.\n\n%s" % (str(e), x))
			else:
				raise


class CancelledPledge(models.Model):
	"""Records when a user cancels a Pledge."""

	created = models.DateTimeField(auto_now_add=True, db_index=True)

	trigger = models.ForeignKey(Trigger, on_delete=models.CASCADE, help_text="The Trigger that the pledge was for.")
	via_campaign = models.ForeignKey('itfsite.Campaign', blank=True, null=True, on_delete=models.CASCADE, help_text="The Campaign that this Pledge was made via.")
	user = models.ForeignKey('itfsite.User', blank=True, null=True, on_delete=models.CASCADE, help_text="The user who made the pledge, if not anonymous.")
	anon_user = models.ForeignKey('itfsite.AnonymousUser', blank=True, null=True, on_delete=models.CASCADE, help_text="When an anonymous user makes a pledge, a one-off object is stored here and we send a confirmation email.")

	pledge = JSONField(blank=True, help_text="The original Pledge information.")

	@staticmethod
	def from_pledge(pledge):
		cp = CancelledPledge()
		cp.trigger = pledge.trigger
		cp.via_campaign = pledge.via_campaign
		cp.user = pledge.user
		cp.anon_user = pledge.anon_user
		cp.pledge = { k: getattr(pledge, k) for k in Pledge.cancel_archive_fields }
		cp.pledge['amount'] = float(cp.pledge['amount']) # can't JSON-serialize a Decimal
		cp.pledge['created'] = cp.pledge['created'].isoformat() # can't JSON-serialize a DateTime
		cp.pledge['updated'] = cp.pledge['updated'].isoformat() # can't JSON-serialize a DateTime
		cp.pledge.update(pledge.profile.extra)
		cp.save()

class IncompletePledge(models.Model):
	"""Records email addresses users enter. Deleted when they finish a Pledge. Max one per email address."""
	created = models.DateTimeField(auto_now_add=True, db_index=True)
	trigger = models.ForeignKey(Trigger, on_delete=models.CASCADE, help_text="The Trigger that the pledge was for.")
	via_campaign = models.ForeignKey('itfsite.Campaign', blank=True, null=True, on_delete=models.CASCADE, help_text="The Campaign that this Pledge was made via.")
	email = models.EmailField(max_length=254, db_index=True, unique=True, help_text="An email address.")
	extra = JSONField(blank=True, help_text="Additional information stored with this object.")
	sent_followup_at = models.DateTimeField(blank=True, null=True, db_index=True, help_text="If we've sent a follow-up email, the date and time we sent it.")
	completed_pledge = models.ForeignKey(Pledge, blank=True, null=True, on_delete=models.CASCADE, help_text="If the user came back and finished a Pledge, that pledge.")

	def get_utm_campaign_string(self):
		# What campaign string do we attach to the URL?
		campaign = 'itf_ip_%d' % self.id
		if self.extra.get('ref_code'):
			campaign += ',' + self.extra.get('ref_code')
		return campaign

	def get_return_url(self):
		# Construct URL of the Campaign the user was on with a utm_campaign
		# query string argument put in that indicates the user was coming back
		# from an IncompletePledge email. Returns a full URL.
		import urllib.parse
		return self.via_campaign.get_short_url() \
			+ "?" + urllib.parse.urlencode({ "utm_campaign": self.get_utm_campaign_string() })

class PledgeExecutionProblem(enum.Enum):
	NoProblem = 0
	EmailUnconfirmed = 1 # email address on the pledge was not confirmed
	FiltersExcludedAll = 2 # no recipient matched filters
	TransactionFailed = 3 # problems making the donation in the DE api
	Voided = 4 # after a successful transaction, user asked us to void it

class PledgeExecution(models.Model):
	"""How a user's pledge was executed. Each pledge has a single PledgeExecution when the Trigger is executed, and immediately many Contribution objects are created."""

	pledge = models.OneToOneField(Pledge, related_name="execution", on_delete=models.PROTECT, help_text="The Pledge this execution information is about.")
	trigger_execution = models.ForeignKey(TriggerExecution, related_name="pledges", on_delete=models.PROTECT, help_text="The TriggerExecution this execution information is about.")

	created = models.DateTimeField(auto_now_add=True, db_index=True)

	problem = EnumField(PledgeExecutionProblem, default=PledgeExecutionProblem.NoProblem, help_text="A problem code associated with a failure to make any contributions for the pledge.")
	charged = models.DecimalField(max_digits=6, decimal_places=2, help_text="The amount the user's account was actually charged, in dollars and including fees. It may differ from the pledge amount to ensure that contributions of whole-cent amounts could be made to candidates.")
	fees = models.DecimalField(max_digits=6, decimal_places=2, help_text="The fees the user was charged, in dollars.")
	extra = JSONField(blank=True, help_text="Additional information stored with this object.")

	district = models.CharField(max_length=4, blank=True, null=True, db_index=True, help_text="The congressional district of the user (at the time of the pledge), in the form of XX00.")

	objects = NoMassDeleteManager()

	def __str__(self):
		return str(self.pledge)

	@transaction.atomic
	def delete(self, really=False, with_void=True):
		# We don't delete PledgeExecutions because they are transactional
		# records. And open Pledges will get executed again automatically,
		# so we can't simply void an execution by deleting this record.
		if not really and self.charged > 0:
			raise ValueError("Can't delete a PledgeExecution. (Set the 'really' flag internally.)")

		# But maybe in debugging/testing we want to be able to delete
		# a pledge execution, so....

		# Void this PledgeExecution, if needed.
		if self.problem == PledgeExecutionProblem.NoProblem:
			self.void(with_void=with_void)

		# Return the Pledge to the open state so we can try to execute again.
		self.pledge.status = PledgeStatus.Open
		self.pledge.save(update_fields=['status'])

		# Decrement the TriggerExecution's pledge_count.
		te = self.pledge.trigger.execution
		te.pledge_count = models.F('pledge_count') - 1
		te.save(update_fields=['pledge_count'])

		# Delete record.
		super(PledgeExecution, self).delete()	

	def show_txn(self):
		import rtyaml
		from contrib.bizlogic import DemocracyEngineAPI
		txns = set(item['transaction_guid'] for item in self.extra['donation']['line_items'])
		for txn in txns:
			print(rtyaml.dump(DemocracyEngineAPI.get_transaction(txn)))

	@property
	def problem_text(self):
		if self.problem == PledgeExecutionProblem.EmailUnconfirmed:
			return "Your contribution was not made because you did not confirm your email address before %s." \
				% self.pledge.trigger.trigger_type.strings['retrospective_vp']
		if self.problem == PledgeExecutionProblem.TransactionFailed:
			return "There was a problem charging your credit card and making the contribution: %s. Your contribution could not be made." \
				% self.pledge.execution.extra['exception']
		if self.problem == PledgeExecutionProblem.FiltersExcludedAll:
			return "Your contribution was not made because there were no %s that met your criteria of %s." \
				% (self.pledge.trigger.trigger_type.strings['actors'], self.pledge.targets_summary)
		if self.problem == PledgeExecutionProblem.Voided:
			return "We cancelled your contribution per your request."

	@transaction.atomic
	def void(self, with_void=True):
		# A user has asked us to void a transaction.

		# Is there anything to void?
		if self.problem != PledgeExecutionProblem.NoProblem:
			raise ValueError("Can't void a pledge in state %s." % str(self.problem))
		if not self.extra["donation"]: # sanity check
			raise ValueError("Can't void a pledge that doesn't have an actual donation.")
		
		# Take care of database things first. Let any of these
		# things fail before we call out to DE.

		# Delete the contributions explicitly so that .delete() gets called (by our manager).
		self.contributions.all().delete()

		# Decrement the TriggerExecution's count of successful pledge executions
		# (incremented only for NoProblem executions).
		te = self.pledge.trigger.execution
		te.pledge_count_with_contribs = models.F('pledge_count_with_contribs') - 1
		te.save(update_fields=['pledge_count_with_contribs'])

		# Change the status of this PledgeExecution.
		de_don = self.extra['donation']
		self.extra['voided_donation'] = self.extra['donation']
		del self.extra['donation']
		self.problem = PledgeExecutionProblem.Voided
		self.save()

		# In debugging, we don't bother calling Democracy Engine to void
		# the transaction. It might fail if the record is very old.
		if not with_void:
			return

		# Void or refund the transaction. There should be only one, but
		# just in case get a list of all mentioned transactions for the
		# donation. Do this last so that if the void succeeds no other
		# error can follow.
		void = []
		txns = set(item['transaction_guid'] for item in de_don['line_items'])
		for txn in txns:
			# Raises an exception on failure.
			ret = void_pledge_transaction(txn, allow_credit=True)
			void.append(ret)

		# Store void result.
		self.extra['void'] = void
		self.save()

		return void

	@transaction.atomic
	def update_district(self, district, other):
		# lock so we don't overwrite
		self = PledgeExecution.objects.filter(id=self.id).select_for_update().get()

		# temporarily decrement all of the contributions from the aggregates
		for c in self.contributions.all():
			c.update_aggregates(factor=-1)

		self.district = district
		self.extra['geocode'] = other
		self.save(update_fields=['district', 'extra'])

		# re-increment now that the district is set
		for c in self.contributions.all():
			c.update_aggregates(factor=1)

class Tip(models.Model):
	"""A tip to an Organization made while making a Pledge."""

	user = models.ForeignKey('itfsite.User', blank=True, null=True, on_delete=models.PROTECT, help_text="The user making the Tip.")
	profile = models.ForeignKey(ContributorInfo, related_name="tips", on_delete=models.PROTECT, help_text="The contributor information (name, address, etc.) and billing information used for this Tip.")
	amount = models.DecimalField(max_digits=6, decimal_places=2, help_text="The amount of the tip, in dollars.")
	recipient = models.ForeignKey('itfsite.Organization', on_delete=models.PROTECT, help_text="The recipient of the tip.")

	de_recip_id = models.CharField(max_length=64, blank=True, null=True, db_index=True, help_text="The recipient ID on Democracy Engine that received the tip.")

	via_campaign = models.ForeignKey('itfsite.Campaign', blank=True, null=True, related_name="tips", on_delete=models.PROTECT, help_text="The Campaign that this Tip was made via.")
	via_pledge = models.OneToOneField(Pledge, blank=True, null=True, related_name="tip", on_delete=models.PROTECT, help_text="The executed Pledge that this Tip was made via.")
	ref_code = models.CharField(max_length=24, blank=True, null=True, db_index=True, help_text="An optional referral code that lead the user to take this action.")

	created = models.DateTimeField(auto_now_add=True, db_index=True)
	updated = models.DateTimeField(auto_now=True)

	extra = JSONField(blank=True, help_text="Additional information stored with this object.")

	def save(self, *args, override_immutable_check=False, **kwargs):
		if self.id and not override_immutable_check:
			raise Exception("This model is immutable.")
		super().save(*args, **kwargs)

	@staticmethod
	def execute_from_pledge(pledge):
		# Validate.
		if not pledge.user: raise ValueError("Pledge was made by an unconfirmed user.")
		if pledge.tip_to_campaign_owner == 0: raise ValueError("Pledge does not specify a tip.")
		if not pledge.via_campaign.owner: raise ValueError("Campaign has no owner.")
		if not pledge.via_campaign.owner.de_recip_id: raise ValueError("Campaign owner has no recipient id.")

		# Create instance.
		tip = Tip()

		tip.user = pledge.user
		tip.profile = pledge.profile

		tip.amount = pledge.tip_to_campaign_owner

		tip.via_pledge = pledge
		tip.via_campaign = pledge.via_campaign
		tip.recipient = pledge.via_campaign.owner
		tip.de_recip_id = pledge.via_campaign.owner.de_recip_id

		tip.extra = {
			"donation": None,
			"exception": "Not yet executed.",
		}

		tip.execute() # also saves

		return tip

	def execute(self):
		import rtyaml
		from contrib.bizlogic import create_de_donation_basic_dict, DemocracyEngineAPI

		# Prepare the donation record for authorization & capture.
		de_don_req = create_de_donation_basic_dict(self.via_pledge)
		de_don_req.update({
			# billing info
			"token": self.profile.extra['billing']['de_cc_token'],

			# line items
			"line_items": [{
				"recipient_id": self.de_recip_id,
				"amount": DemocracyEngineAPI.format_decimal(self.amount),
				}],

			# reported to the recipient
			"source_code": self.via_campaign.get_short_url(),
			"ref_code": "",

			# tracking info for internal use
			"aux_data": rtyaml.dump({ # DE will gives this back to us encoded as YAML, but the dict encoding is ruby-ish so to be sure we can parse it, we'll encode it first
				"via": self.via_campaign.id,
				"pledge": self.via_pledge.id,
				"user": self.user.id,
				"email": self.user.email,
				"pledge_created": self.via_pledge.created,
				})
			})

		# Create the 'donation', which creates a transaction and performs cc authorization.
		try:
			don = DemocracyEngineAPI.create_donation(de_don_req)
			self.extra["donation"] = don
			self.extra["exception"] = None
		except HumanReadableValidationError as e:
			self.extra["exception"] = str(e)

		self.save()

#####################################################################
#
# Recipients and Contributions
#
# Actual campaign contributions made.
#
#####################################################################

class Recipient(models.Model):
	"""A contribution recipient, with the current Democracy Engine recipient ID, which is either an Actor (an incumbent) or a logically specified general election candidate by office sought and party."""

	de_id = models.CharField(max_length=64, unique=True, help_text="The Democracy Engine ID that we have assigned to this recipient.")
	active = models.BooleanField(default=True, help_text="Whether this Recipient can currently receive funds.")

	# this is only set when the recipient is an incumbent - this is the candidate's Actor object
	actor = models.OneToOneField(Actor, blank=True, null=True, help_text="The Actor that this recipient corresponds to (i.e. this Recipient is an incumbent).")

	# these fields are only set for generically specified challengers
	office_sought = models.CharField(max_length=7, blank=True, null=True, db_index=True, help_text="For challengers, a code specifying the office sought in the form of 'S-NY-I' (New York class 1 senate seat) or 'H-TX-30' (Texas 30th congressional district). Unique with party.")
	party = EnumField(ActorParty, blank=True, null=True, help_text="The party of the challenger, or null if this Recipient is for an incumbent. Unique with office_sought.")

	class Meta:
		unique_together = [('office_sought', 'party')]

	def __str__(self):
		if self.actor:
			# is an incumbent
			return str(self.actor)
		else:
			try:
				# is a currently challenger of someone
				return self.party.name + " Challenger to " + str(self.challenger_to) + " (" + self.office_sought + ")"
			except:
				# is not a current challenger of someone, so just use office/party designation
				return self.office_sought + ":" + str(self.party)

	@property
	def is_challenger(self):
		return self.actor is None

class ContributionRecipientType(enum.Enum):
	Null = 0
	Incumbent = 1 # the Actor that took the Action, i.e. the incumbent
	GeneralChallenger = 2 # the Actor's general election challenger

class Contribution(models.Model):
	"""A fully executed campaign contribution."""

	pledge_execution = models.ForeignKey(PledgeExecution, related_name="contributions", on_delete=models.PROTECT, help_text="The PledgeExecution this execution information is about.")
	action = models.ForeignKey(Action, on_delete=models.PROTECT, help_text="The Action this contribution was made in reaction to.")
	recipient_type = EnumField(ContributionRecipientType, default=ContributionRecipientType.Null, help_text="The logical specification of the recipient, i.e. the Actor (incumbent) or a general election challenger of the Actor.")
	recipient = models.ForeignKey(Recipient, related_name="contributions", on_delete=models.PROTECT, help_text="The Recipient this contribution was sent to.")
	amount = models.DecimalField(max_digits=6, decimal_places=2, help_text="The amount of the contribution, in dollars.")
	refunded_time = models.DateTimeField(blank=True, null=True, help_text="If the contribution was refunded to the user, the time that happened.")

	de_id = models.CharField(max_length=64, help_text="The Democracy Engine ID that the contribution was assigned to.")

	extra = JSONField(blank=True, help_text="Additional information about the contribution.")

	objects = NoMassDeleteManager()

	class Meta:
		# Each PledgeExecution can have at most one Contribution for an Action,
		# but because a PledgeExecution can include Actions from multiple TriggerExecutions
		# there may be repeated Recipients for the same PledgeExecution.
		unique_together = [('pledge_execution', 'action')]

	def __str__(self):
		return "$%0.2f to %s for %s" % (self.amount, self.recipient, self.pledge_execution)

	def name_long(self):
		if self.recipient_type == ContributionRecipientType.Incumbent:
			# is an incumbent
			return self.action.name_long
		else:
			# is a challenger, but who it was a challenger to may be different
			# from who the recipient is a challenger to now, so use the action
			# to get the name of the incumbent.
			return self.recipient.party.name + " Challenger to " + self.action.name_long

	@transaction.atomic
	def delete(self):
		# Delete this object. You almost certainly do NOT want to do this
		# since the transaction line item will remain on the Democracy
		# Engine side.

		# Decrement the TriggerExecution and Action's total_pledged fields.
		self.update_aggregates(factor=-1)

		# Remove record.
		super(Contribution, self).delete()	

	def update_aggregates(self, factor=1, updater=None):
		# Increment the totals on the Action instance. This excludes fees because
		# this is based on transaction line items.
		if self.recipient_type == ContributionRecipientType.Incumbent:
			# Contribution was to the Actor.
			field = 'total_contributions_for'
		else:
			# Contribution was to the Actor's opponent.
			field = 'total_contributions_against'
		setattr(self.action, field, models.F(field) + self.amount*factor)
		self.action.save(update_fields=[field])

		# Increment the TriggerExecution's total_contributions. Likewise, it
		# excludes fees. When a Pledge uses the Actions of multiple Triggers,
		# then self.pledge_execution.execution != self.action.execution (both
		# a TriggerExecution). In that case, we (double-)count the totals in
		# each.
		triggerexecutions = [self.pledge_execution.trigger_execution]
		if triggerexecutions[0] != self.action.execution:
			triggerexecutions.append(self.action.execution)
		for te in triggerexecutions:
			te.total_contributions = models.F('total_contributions') + self.amount*factor
			te.num_contributions = models.F('num_contributions') + 1*factor
			te.save(update_fields=['total_contributions', 'num_contributions'])

	@staticmethod
	def aggregate(*across, **kwargs):
		# Expand field aliases. Each alias is a tuple of:
		#  ((field, lookup), to-database-value, from-database-value)
		aliases = {
			# trigger and desired_outcome work off of the PledgeExecution's pledge. For multi-trigger
			# Pledges, this is the Trigger that the Pledge is mainly tied to, not the sub-triggers.
			"trigger":         ("pledge_execution__trigger_execution",       lambda v : v.execution, lambda v : v.trigger),
			"desired_outcome": ("pledge_execution__pledge__desired_outcome", lambda v : v,           lambda v : v),
			
			"actor":           ("action__actor",                             lambda v : v,           lambda v : v),

			# recipient_type needs a mapping from integers (returned by .values())
			# back to enum members.
			"recipient_type":  ("recipient_type",                            lambda v : v,           lambda v : ContributionRecipientType(v)),
		}
		def getalias(a): return aliases.get(a, (a, lambda v : v, lambda v : v))
		original_across = across
		across = [ getalias(a)[0] for a in across]
		kwargs = { getalias(k)[0]: getalias(k)[1](v) for (k, v) in kwargs.items() }

		# Apply filters.
		contribs = Contribution.objects.filter(**kwargs)

		if len(across) == 0:
			# Return a tuple (count, amount) for contributions matching the
			# keyword arguments, which are passed directly to Contribution.objects.filter.
			# models.Count always gives an integer, but models.Sum can give None
			# if there the count is zero, so force to 0.0 if it's None.

			ret = contribs.aggregate(count=models.Count('id'), amount=models.Sum('amount'))
			return (ret["count"], ret["amount"] or decimal.Decimal(0))
		
		else:
			# Return a list of (value, (count, amount)) for contributions
			# matching the keyword arguments and where value is a tuple
			# from the cartesian product of the values of the fields in
			# `across`, in the same order. The keyword arguments are passed
			# directly to Contribution.objects.filter.

			qs = contribs\
				.values(*across)\
				.annotate(count=models.Count('id'), amount=models.Sum('amount'))

			# map IDs back to object instances by getting the instances
			# ahead of time in bulk
			if "action" in original_across:
				actions = Action.objects.select_related('actor', 'execution', 'execution__trigger').in_bulk(item["action"] for item in qs)
			if "actor" in original_across:
				actors = Actor.objects.in_bulk(item["action__actor"] for item in qs)
			if "recipient" in original_across:
				recipients = Recipient.objects.in_bulk(item["recipient"] for item in qs)

			# map IDs to object instances or, for aliases, apply the
			# alias inverse function
			def niceval(value, field):
				if field == "action":
					return actions[value]
				elif field == "actor":
					return actors[value]
				elif field == "recipient":
					return recipients[value]
				elif field in ("action__party", "recipient__party"):
					return ActorParty(value)
				else:
					return getalias(field)[2](value)

			# build up the list to return
			ret = []
			for item in qs:
				ret.append( (tuple(niceval(item[a2], a1) for (a1, a2) in zip(original_across, across) ), (item['count'], item['amount'])))

			# sort by amount, descending
			ret.sort(key = lambda item : item[1][1], reverse=True)

			return ret