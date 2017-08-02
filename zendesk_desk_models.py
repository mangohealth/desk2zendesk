from schematics.models import Model
from schematics.types import StringType, DateTimeType, IntType, URLType, BooleanType
from schematics.types.compound import ListType, ModelType
import datetime
from django.utils.encoding import force_bytes
import logging
from urlparse import urlsplit

logger = logging.getLogger("migrate_to_zendesk")


class FBUser(Model):
    image_url = URLType()
    profile_url = URLType()


class TwitUser(Model):
    handle = StringType()
    image_url = URLType()


class Email(Model):
    value = StringType()
    type = StringType()


class User(Model):
    """User from Desk."""

    id = StringType(required=True)  # Desk uses string identifiers
    first_name = StringType(required=True)
    last_name = StringType(required=True)
    avatar = URLType(default="")
    emails = ListType(ModelType(Email), default=list)
    phone_numbers = ListType(StringType, default=list)
    addresses = ListType(StringType, default=list)
    # From embedded
    facebook_user = ModelType(FBUser, default=None)
    twitter_user = ModelType(TwitUser, default=None)


class ZIdentity(Model):
    """Identities in Zendesk; FB, Twitter, or emails from Desk."""

    type = StringType(required=True)
    value = StringType(required=True)
    verified = BooleanType(required=True)  # Always true for migration - no email


class ZUser(Model):
    name = StringType(required=True)
    email = StringType(default="")
    role = StringType(required=True)
    external_id = StringType(required=True)  # Desk ID
    identities = ListType(ModelType(ZIdentity), default=list)
    remote_photo_url = StringType(default="")
    verified = BooleanType(required=True)
    tags = ListType(StringType, default=list)

    def desk_user_to_ZUser(self, user):
        """Convert Desk User object to Zendesk ZUser object."""
        self.name = user.first_name + ' ' + user.last_name
        if user.emails:
            self.email = user.emails[0].value
        else:
            self.email = None
        self.role = 'end-user'
        self.external_id = user.id
        zidentities = []
        zidentities.extend((ZIdentity({'type': 'email', 'value': email.value,
                                      'verified': True}, strict=False)) for email in user.emails[1:])
        if user.twitter_user:
            zidentities.append(ZIdentity(
                {'type': 'twitter', 'value': user.twitter_user.handle, 'verified': True}, strict=False))
        if user.facebook_user:
            zidentities.append(ZIdentity(
                {'type': 'facebook', 'value': get_fb_id_from_photo(user.facebook_user.image_url), 'verified': True}, strict=False))
        self.identities = zidentities
        self.verified = True
        self.remote_photo_url = user.avatar
        self.tags = ['desk_user_before_%s' % str(datetime.datetime.now().date())]

        return self


def get_fb_id_from_photo(url):
    str_url = force_bytes(url)
    parsed = urlsplit(str_url)  # urlsplit doesn't work with unicode strings
    logger.info(parsed)
    if parsed.netloc != 'graph.facebook.com':
        logger.error("Photo URL isn't from facebook - falling back on using entire URL %s" % str_url)
        return url
    split = parsed.path.split('/')  # url path looks like /<number>/picture
    if len(split) == 3:
        try:
            int(split[1])
            return split[1]
        except ValueError:
            pass
    logger.error("Photo URL doesn't have ID - falling back on using entire URL %s" % str_url)
    return url


class Message(Model):
    direction = StringType(required=True)
    body = StringType(required=True, default="")
    updated_at = DateTimeType(formats='%Y-%m-%dT%H:%M:%SZ', required=True)
    status = StringType(required=True)
    # From embedded
    uri = StringType(required=True)  # can't use message ID: != ID from attachments
    creator_id = IntType(required=True)


class ZMessage(Model):
    author_id = IntType(required=True)
    created_at = DateTimeType(formats='%Y-%m-%dT%H:%M:%SZ', required=True)
    uploads = ListType(StringType, default=list)  # list of upload tokens
    public = BooleanType(required=True)


class ZMessageCreate(ZMessage):
    value = StringType(required=True, default="")


class ZMessageUpdate(ZMessage):
    body = StringType(required=True, default="")


class Attachment(Model):
    file_name = StringType(required=True)
    url = URLType(required=True)
    # From embedded
    message_uri = StringType(required=False)


class Ticket(Model):
    id = IntType(required=True)
    subject = StringType(default="")
    priority = IntType(required=True)
    blurb = StringType(default="")
    status = StringType(required=True)
    created_at = DateTimeType(formats='%Y-%m-%dT%H:%M:%SZ', required=True)
    updated_at = DateTimeType(formats='%Y-%m-%dT%H:%M:%SZ', required=False)
    resolved_at = DateTimeType(formats='%Y-%m-%dT%H:%M:%SZ', required=False)
    # From embedded
    user_id = IntType(required=True)
    messages = ListType(ModelType(Message), default=list)
    attachments = ListType(ModelType(Attachment), default=list)
    notes = ListType(ModelType(Message), default=list)
    num_replies = IntType(required=True)
    num_notes = IntType(required=True)
    num_attachments = IntType(required=True)


class ZTicket(Model):
    id = IntType(required=False)  # Only complete update ticket will have id
    subject = StringType(default="")
    priority = StringType(required=True)  # translate from Desk 1-10
    description = StringType(default="")
    status = StringType(required=True)
    created_at = DateTimeType(formats='%Y-%m-%dT%H:%M:%SZ', required=True)
    updated_at = DateTimeType(formats='%Y-%m-%dT%H:%M:%SZ', required=False)
    solved_at = DateTimeType(formats='%Y-%m-%dT%H:%M:%SZ', required=False)
    external_id = IntType(required=True)  # Desk ID
    requester_id = IntType(required=True)
    assignee_id = IntType(default="")
    comments = ListType(ModelType(ZMessageCreate), default=list)
    tags = ListType(StringType, default=list)


class ZTicketUpdate(Model):
    id = IntType(required=True)
    # API for updating requires single comment
    comment = ModelType(ZMessageUpdate, required=True)
