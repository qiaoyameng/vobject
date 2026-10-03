"""Definitions and behavior for vCard 4.0 (RFC 6350).

vCard 4.0 differs from 3.0 in ways that go beyond the VERSION string:

* properties have well-defined default VALUE types (TEL and PHOTO default
  to URIs, BDAY and ANNIVERSARY to date-and-or-time),
* PREF, LANGUAGE, ALTID and PID are real parameters, replacing the 3.0
  TYPE=PREF convention,
* parameter values use the caret escaping defined in RFC 6868,
* the ENCODING and CHARSET parameters no longer exist,
* dates may be partial, allowing yearless birthdays like --0415.

The behaviors in this module are registered under the '4.0' id, so the
VERSION line of a vCard selects them for parsing, validation and
serialization.  vCard 3.0 behavior (vobject.vcard) is left untouched.
"""

import datetime
import re

from .base import (ContentLine, ParseError, ValidateError, backslashEscape,
                   registerBehavior)
from .icalendar import stringToTextValues
from .vcard import (AddressBehavior, NameBehavior, VCardBehavior)


# --------------------- RFC 6868 parameter value escaping ---------------------

_PARAM_UNESCAPES = {'^n': '\n', '^N': '\n', '^^': '^', "^'": '"'}
_param_escape_re = re.compile(r'\^.')


def escapeParamValue(value):
    """
    Escape a parameter value as defined in RFC 6868 (vCard 4.0).
    """
    return value.replace('^', '^^').replace('\n', '^n').replace('"', "^'")


def unescapeParamValue(value):
    """
    Inverse of escapeParamValue.  Unknown escape sequences are left intact.
    """
    return _param_escape_re.sub(
        lambda match: _PARAM_UNESCAPES.get(match.group(0), match.group(0)),
        value)


# ------------------------- date-and-or-time values ---------------------------

_date_complete_re = re.compile(r'^(\d{4})(\d{2})(\d{2})$')
_datetime_complete_re = re.compile(
    r'^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z|[+-]\d{4})?$')
_date_noyear_re = re.compile(r'^--(\d{2})(\d{2})$')
_date_dayonly_re = re.compile(r'^---(\d{2})$')
_time_re = re.compile(
    r'^T(\d{6}|\d{4}|\d{2}|-\d{4}|-\d{2}|--\d{2})(Z|[+-]\d{4})?$')


def _validMonthDay(month, day):
    return 1 <= month <= 12 and 1 <= day <= 31


def _isValidDate(value):
    match = _date_complete_re.match(value)
    if match:
        try:
            datetime.date(*[int(group) for group in match.groups()])
            return True
        except ValueError:
            return False
    match = _date_noyear_re.match(value)
    if match:
        return _validMonthDay(int(match.group(1)), int(match.group(2)))
    match = _date_dayonly_re.match(value)
    if match:
        return 1 <= int(match.group(1)) <= 31
    return False


def _isValidTime(value):
    return _time_re.match(value) is not None


def isValidDateAndOrTime(value):
    """
    Return True if value is a valid RFC 6350 date-and-or-time, including
    partial representations like --0415 (a yearless birthday).
    """
    date_part, separator, time_part = value.partition('T')
    if not separator:
        return _isValidDate(date_part)
    if date_part and not _isValidDate(date_part):
        return False
    return _isValidTime('T' + time_part)


def _parseUtcOffset(offset):
    if offset == 'Z':
        return datetime.timezone.utc
    sign = 1 if offset[0] == '+' else -1
    delta = datetime.timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
    return datetime.timezone(sign * delta)


def stringToDateAndOrTime(value):
    """
    Parse an RFC 6350 date-and-or-time value.

    Complete dates become datetime.date instances and complete date-times
    become datetime.datetime instances.  Partial values (e.g. the yearless
    birthday --0415) are validated and returned as strings.
    """
    match = _date_complete_re.match(value)
    if match:
        try:
            return datetime.date(*[int(group) for group in match.groups()])
        except ValueError:
            raise ParseError("{0!r} is not a valid date".format(value))
    match = _datetime_complete_re.match(value)
    if match:
        groups = match.groups()
        tzinfo = _parseUtcOffset(groups[6]) if groups[6] else None
        try:
            return datetime.datetime(
                *[int(group) for group in groups[:6]], tzinfo=tzinfo)
        except ValueError:
            raise ParseError("{0!r} is not a valid date-time".format(value))
    if isValidDateAndOrTime(value):
        return value
    raise ParseError("{0!r} is not a valid date-and-or-time".format(value))


def dateAndOrTimeToString(value):
    """
    Serialize a date, datetime or partial date-and-or-time string.
    """
    if isinstance(value, datetime.datetime):
        out = value.strftime('%Y%m%dT%H%M%S')
        offset = value.utcoffset() if value.tzinfo is not None else None
        if offset is not None:
            if offset == datetime.timedelta(0):
                out += 'Z'
            else:
                sign = '+' if offset >= datetime.timedelta(0) else '-'
                offset = abs(offset)
                hours = offset.days * 24 + offset.seconds // 3600
                minutes = (offset.seconds // 60) % 60
                out += '{0}{1:02d}{2:02d}'.format(sign, hours, minutes)
        return out
    if isinstance(value, datetime.date):
        return value.strftime('%Y%m%d')
    return value


# ------------------------------ helpers --------------------------------------

def _invalidate(line, msg, raiseException):
    """
    Report a validation failure, locating the offending property by name
    and, when known, by line number.
    """
    if raiseException:
        raise ValidateError("{0}: {1}".format(line.name, msg),
                            getattr(line, 'lineNumber', None))
    return False


_language_tag_re = re.compile(r'^[A-Za-z]{1,8}(-[A-Za-z0-9]{1,8})*$')
_uri_scheme_re = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*:')


# ------------------------ registered behavior classes ------------------------

class VCard4Behavior(VCardBehavior):
    """
    Base behavior for vCard 4.0 properties.

    Handles RFC 6868 parameter escaping and validation of the parameters
    common to all 4.0 properties (PREF, LANGUAGE, ALTID, ...).
    """
    allowGroup = True
    versionString = '4.0'
    allowedValueTypes = ()

    @classmethod
    def decode(cls, line):
        if line.encoded:
            for key, values in line.params.items():
                line.params[key] = [unescapeParamValue(v) for v in values]
            line.encoded = False

    @classmethod
    def encode(cls, line):
        if not line.encoded:
            for key, values in line.params.items():
                line.params[key] = [escapeParamValue(v) for v in values]
            line.encoded = True

    @classmethod
    def lineValidate(cls, line, raiseException, complainUnrecognized):
        if 'ENCODING' in line.params or 'CHARSET' in line.params:
            return _invalidate(
                line,
                'ENCODING and CHARSET parameters were removed in vCard 4.0',
                raiseException)
        if cls.allowedValueTypes and 'VALUE' in line.params:
            value_type = str(line.params['VALUE'][0]).upper()
            if value_type not in cls.allowedValueTypes:
                return _invalidate(
                    line,
                    'VALUE parameter must be {0}, got {1!s}'.format(
                        ' or '.join(cls.allowedValueTypes),
                        line.params['VALUE'][0]),
                    raiseException)
        for pref in line.params.get('PREF', []):
            try:
                pref_value = int(pref)
            except (TypeError, ValueError):
                return _invalidate(
                    line,
                    'PREF parameter must be an integer between 1 and 100, '
                    'got {0!r}'.format(pref),
                    raiseException)
            if not 1 <= pref_value <= 100:
                return _invalidate(
                    line,
                    'PREF parameter must be between 1 and 100, '
                    'got {0!r}'.format(pref),
                    raiseException)
        for language in line.params.get('LANGUAGE', []):
            if not _language_tag_re.match(str(language)):
                return _invalidate(
                    line,
                    'LANGUAGE parameter {0!r} is not a valid language tag'
                    .format(language),
                    raiseException)
        return True


class VCard4TextBehavior(VCard4Behavior):
    """
    Text-valued vCard 4.0 property (the default VALUE type for FN, EMAIL,
    KIND, ...).  There is no base64 ENCODING in 4.0, only backslash escaping.
    """
    allowedValueTypes = ('TEXT',)

    @classmethod
    def decode(cls, line):
        if line.encoded:
            VCard4Behavior.decode(line)
            line.value = stringToTextValues(line.value)[0]

    @classmethod
    def encode(cls, line):
        if not line.encoded:
            line.value = backslashEscape(line.value)
            VCard4Behavior.encode(line)


class VCard4URIBehavior(VCard4Behavior):
    """
    Property whose default VALUE type is URI (TEL, PHOTO, MEMBER, RELATED).
    """
    allowTextValue = False
    allowedValueTypes = ('URI', 'TEXT')

    @classmethod
    def lineValidate(cls, line, raiseException, complainUnrecognized):
        if not VCard4Behavior.lineValidate(line, raiseException,
                                           complainUnrecognized):
            return False
        value_types = line.params.get('VALUE')
        is_text = (value_types is not None and
                   str(value_types[0]).upper() == 'TEXT')
        if is_text:
            if not cls.allowTextValue:
                return _invalidate(
                    line,
                    'VALUE=TEXT is not allowed for {0}'.format(line.name),
                    raiseException)
        elif not _uri_scheme_re.match(str(line.value)):
            return _invalidate(
                line,
                'value {0!r} is not a URI; the default VALUE type of {1} '
                'is URI, use VALUE=TEXT for free text'.format(line.value,
                                                              line.name),
                raiseException)
        return True


class DateAndOrTimeBehavior(VCard4Behavior):
    """
    BDAY and ANNIVERSARY: date-and-or-time by default, VALUE=TEXT allowed.
    Partial dates (yearless birthdays like --0415) stay strings.
    """
    hasNative = True
    allowedValueTypes = ('DATE-AND-OR-TIME', 'TEXT')

    @staticmethod
    def _isTextValue(obj):
        value_types = obj.params.get('VALUE')
        return (value_types is not None and
                str(value_types[0]).upper() == 'TEXT')

    @staticmethod
    def transformToNative(obj):
        """
        Turn obj.value into a date, datetime, or validated partial string.
        """
        if obj.isNative:
            return obj
        obj.isNative = True
        if obj.value == '':
            return obj
        if DateAndOrTimeBehavior._isTextValue(obj):
            obj.value = stringToTextValues(obj.value)[0]
            return obj
        obj.value = stringToDateAndOrTime(obj.value)
        return obj

    @staticmethod
    def transformFromNative(obj):
        """
        Replace the date/datetime in obj.value with an RFC 6350 string.
        """
        if obj.isNative:
            obj.isNative = False
            if DateAndOrTimeBehavior._isTextValue(obj):
                obj.value = backslashEscape(obj.value)
            else:
                obj.value = dateAndOrTimeToString(obj.value)
        return obj

    @classmethod
    def lineValidate(cls, line, raiseException, complainUnrecognized):
        if not VCard4Behavior.lineValidate(line, raiseException,
                                           complainUnrecognized):
            return False
        if DateAndOrTimeBehavior._isTextValue(line):
            return True
        value = line.value
        if isinstance(value, (datetime.date, datetime.datetime)):
            return True
        if not value:
            return _invalidate(line, 'a value is required', raiseException)
        if not isValidDateAndOrTime(str(value)):
            return _invalidate(
                line,
                'value {0!r} is not a valid date-and-or-time; use '
                'VALUE=TEXT for free text'.format(value),
                raiseException)
        return True


class VCard4_0(VCardBehavior):
    """
    vCard 4.0 behavior, defined in RFC 6350.
    """
    name = 'VCARD'
    description = 'vCard 4.0, defined in rfc6350'
    versionString = '4.0'
    isComponent = True
    sortFirst = ('version', 'prodid', 'uid')
    defaultBehavior = VCard4TextBehavior
    knownChildren = {
        'VERSION':    (1, 1, None),  # required, auto-generated
        'FN':         (1, None, '4.0'),
        'N':          (0, 1, '4.0'),
        'ADR':        (0, None, '4.0'),
        'TEL':        (0, None, '4.0'),
        'EMAIL':      (0, None, '4.0'),
        'PHOTO':      (0, None, '4.0'),
        'BDAY':       (0, 1, '4.0'),
        'ANNIVERSARY': (0, 1, '4.0'),
        'KIND':       (0, 1, '4.0'),
        'RELATED':    (0, None, '4.0'),
        'MEMBER':     (0, None, '4.0'),
        'PRODID':     (0, 1, None),
        'UID':        (0, 1, None),
        'REV':        (0, 1, None),
        'ORG':        (0, None, None),
        'NICKNAME':   (0, None, None),
        'CATEGORIES': (0, None, None),
        'NOTE':       (0, None, None),
        'URL':        (0, None, None),
    }

    @classmethod
    def generateImplicitParameters(cls, obj):
        """
        Create VERSION if needed.  A VERSION provided by the user, even one
        naming a different version, is never overwritten.
        """
        if not hasattr(obj, 'version'):
            obj.add(ContentLine('VERSION', [], cls.versionString))

    @classmethod
    def validate(cls, obj, raiseException=False, complainUnrecognized=False):
        if not super(VCard4_0, cls).validate(obj, raiseException,
                                             complainUnrecognized):
            return False
        if 'member' in obj.contents:
            kind = obj.getChildValue('kind', 'individual')
            if str(kind).lower() != 'group':
                if raiseException:
                    raise ValidateError(
                        'VCARD: MEMBER is only allowed in a vCard with '
                        'KIND:group, got KIND:{0!s}'.format(kind))
                return False
        return True
registerBehavior(VCard4_0)


class FN(VCard4TextBehavior):
    name = 'FN'
    description = 'Formatted name'
registerBehavior(FN, 'FN')


class EMAIL(VCard4TextBehavior):
    name = 'EMAIL'
    description = 'Electronic mail address'
registerBehavior(EMAIL, 'EMAIL')


class KIND(VCard4TextBehavior):
    name = 'KIND'
    description = 'Kind of entity the vCard represents'
    knownKinds = ('individual', 'group', 'org', 'location')

    @classmethod
    def lineValidate(cls, line, raiseException, complainUnrecognized):
        if not VCard4TextBehavior.lineValidate(line, raiseException,
                                               complainUnrecognized):
            return False
        kind = str(line.value).lower()
        if kind not in cls.knownKinds and not kind.startswith('x-'):
            return _invalidate(
                line,
                'KIND must be one of {0} or an x-name, got {1!r}'.format(
                    ', '.join(cls.knownKinds), line.value),
                raiseException)
        return True
registerBehavior(KIND, 'KIND')


class TEL(VCard4URIBehavior):
    name = 'TEL'
    description = 'Telephone number, a tel: URI by default'
    allowTextValue = True
registerBehavior(TEL, 'TEL')


class PHOTO(VCard4URIBehavior):
    name = 'PHOTO'
    description = 'Photograph, a URI (often a data: URI) in vCard 4.0'

    @classmethod
    def valueRepr(cls, line):
        value = str(line.value)
        if len(value) > 64:
            return value[:61] + '...'
        return value
registerBehavior(PHOTO, 'PHOTO')


class MEMBER(VCard4URIBehavior):
    name = 'MEMBER'
    description = 'Member of a group vCard, a URI (VALUE=TEXT not allowed)'
registerBehavior(MEMBER, 'MEMBER')


class RELATED(VCard4URIBehavior):
    name = 'RELATED'
    description = 'Related entity, a URI by default'
    allowTextValue = True
registerBehavior(RELATED, 'RELATED')


class BDAY(DateAndOrTimeBehavior):
    name = 'BDAY'
    description = 'Birth date, a date-and-or-time (possibly partial)'
registerBehavior(BDAY, 'BDAY')


class ANNIVERSARY(DateAndOrTimeBehavior):
    name = 'ANNIVERSARY'
    description = 'Anniversary date, a date-and-or-time (possibly partial)'
registerBehavior(ANNIVERSARY, 'ANNIVERSARY')


class NameBehavior4(NameBehavior, VCard4Behavior):
    """
    vCard 4.0 structured name.  Same Name interface as 3.0.
    """
    versionString = '4.0'
    allowedValueTypes = ('TEXT',)
registerBehavior(NameBehavior4, 'N')


class AddressBehavior4(AddressBehavior, VCard4Behavior):
    """
    vCard 4.0 structured address.  Same Address interface as 3.0;
    the delivery label is now the LABEL parameter, not a LABEL property.
    """
    versionString = '4.0'
    allowedValueTypes = ('TEXT',)
registerBehavior(AddressBehavior4, 'ADR')
