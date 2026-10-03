"""Definitions and behavior for vCard 3.0 and vCard 4.0"""

import codecs
import datetime
import re

from . import behavior

from .base import ContentLine, Component, registerBehavior, backslashEscape, \
    str_, ValidateError
from .icalendar import stringToTextValues, stringToDate, stringToDateTime, \
    dateToString, dateTimeToString


# Python 3 no longer has a basestring type, so....
try:
    basestring = basestring
except NameError:
    basestring = (str, bytes)

# ------------------------ vCard structs ---------------------------------------


class Name(object):
    def __init__(self, family='', given='', additional='', prefix='',
                 suffix=''):
        """
        Each name attribute can be a string or a list of strings.
        """
        self.family = family
        self.given = given
        self.additional = additional
        self.prefix = prefix
        self.suffix = suffix

    @staticmethod
    def toString(val):
        """
        Turn a string or array value into a string.
        """
        if type(val) in (list, tuple):
            return ' '.join(val)
        return val

    def __str__(self):
        eng_order = ('prefix', 'given', 'additional', 'family', 'suffix')
        out = ' '.join(self.toString(getattr(self, val)) for val in eng_order)
        return str_(out)

    def __repr__(self):
        return "<Name: {0!s}>".format(self.__str__())

    def __eq__(self, other):
        try:
            return (self.family == other.family and
                    self.given == other.given and
                    self.additional == other.additional and
                    self.prefix == other.prefix and
                    self.suffix == other.suffix)
        except:
            return False


class Address(object):
    def __init__(self, street='', city='', region='', code='',
                 country='', box='', extended=''):
        """
        Each name attribute can be a string or a list of strings.
        """
        self.box = box
        self.extended = extended
        self.street = street
        self.city = city
        self.region = region
        self.code = code
        self.country = country

    @staticmethod
    def toString(val, join_char='\n'):
        """
        Turn a string or array value into a string.
        """
        if type(val) in (list, tuple):
            return join_char.join(val)
        return val

    lines = ('box', 'extended', 'street')
    one_line = ('city', 'region', 'code')

    def __str__(self):
        lines = '\n'.join(self.toString(getattr(self, val))
                          for val in self.lines if getattr(self, val))
        one_line = tuple(self.toString(getattr(self, val), ' ')
                         for val in self.one_line)
        lines += "\n{0!s}, {1!s} {2!s}".format(*one_line)
        if self.country:
            lines += '\n' + self.toString(self.country)
        return lines

    def __repr__(self):
        return "<Address: {0!s}>".format(self)

    def __eq__(self, other):
        try:
            return (self.box == other.box and
                    self.extended == other.extended and
                    self.street == other.street and
                    self.city == other.city and
                    self.region == other.region and
                    self.code == other.code and
                    self.country == other.country)
        except:
            return False


# ------------------------ Registered Behavior subclasses ----------------------

class VCardTextBehavior(behavior.Behavior):
    """
    Provide backslash escape encoding/decoding for single valued properties.

    TextBehavior also deals with base64 encoding if the ENCODING parameter is
    explicitly set to BASE64.
    """
    allowGroup = True
    base64string = 'B'

    @classmethod
    def decode(cls, line):
        """
        Remove backslash escaping from line.valueDecode line, either to remove
        backslash espacing, or to decode base64 encoding. The content line should
        contain a ENCODING=b for base64 encoding, but Apple Addressbook seems to
        export a singleton parameter of 'BASE64', which does not match the 3.0
        vCard spec. If we encouter that, then we transform the parameter to
        ENCODING=b
        """
        if line.encoded:
            if 'BASE64' in line.singletonparams:
                line.singletonparams.remove('BASE64')
                line.encoding_param = cls.base64string
            encoding = getattr(line, 'encoding_param', None)
            if encoding:
                if isinstance(line.value, bytes):
                    line.value = codecs.decode(line.value, "base64")
                else:
                    line.value = codecs.decode(line.value.encode("utf-8"), "base64")
            else:
                line.value = stringToTextValues(line.value)[0]
            line.encoded = False

    @classmethod
    def encode(cls, line):
        """
        Backslash escape line.value.
        """
        if not line.encoded:
            encoding = getattr(line, 'encoding_param', None)
            if encoding and encoding.upper() == cls.base64string:
                if isinstance(line.value, bytes):
                    line.value = codecs.encode(line.value, "base64").decode("utf-8").replace('\n', '')
                else:
                    line.value = codecs.encode(line.value.encode(encoding), "base64").decode("utf-8")
            else:
                line.value = backslashEscape(line.value)
            line.encoded = True


class VCardBehavior(behavior.Behavior):
    allowGroup = True
    defaultBehavior = VCardTextBehavior


class VCard3_0(VCardBehavior):
    """
    vCard 3.0 behavior.
    """
    name = 'VCARD'
    description = 'vCard 3.0, defined in rfc2426'
    versionString = '3.0'
    isComponent = True
    sortFirst = ('version', 'prodid', 'uid')
    knownChildren = {
        'N':          (0, 1, None),  # min, max, behaviorRegistry id
        'FN':         (1, None, None),
        'VERSION':    (1, 1, None),  # required, auto-generated
        'PRODID':     (0, 1, None),
        'LABEL':      (0, None, None),
        'UID':        (0, None, None),
        'ADR':        (0, None, None),
        'ORG':        (0, None, None),
        'PHOTO':      (0, None, None),
        'CATEGORIES': (0, None, None)
    }

    @classmethod
    def generateImplicitParameters(cls, obj):
        """
        Create PRODID, VERSION, and VTIMEZONEs if needed.

        VTIMEZONEs will need to exist whenever TZID parameters exist or when
        datetimes with tzinfo exist.
        """
        if not hasattr(obj, 'version'):
            obj.add(ContentLine('VERSION', [], cls.versionString))
registerBehavior(VCard3_0, default=True)


class FN(VCardTextBehavior):
    name = "FN"
    description = 'Formatted name'
registerBehavior(FN)


class Label(VCardTextBehavior):
    name = "Label"
    description = 'Formatted address'
registerBehavior(Label)

wacky_apple_photo_serialize = True
REALLY_LARGE = 1E50


class Photo(VCardTextBehavior):
    name = "Photo"
    description = 'Photograph'

    @classmethod
    def valueRepr(cls, line):
        return " (BINARY PHOTO DATA at 0x{0!s}) ".format(id(line.value))

    @classmethod
    def serialize(cls, obj, buf, lineLength, validate):
        """
        Apple's Address Book is *really* weird with images, it expects
        base64 data to have very specific whitespace.  It seems Address Book
        can handle PHOTO if it's not wrapped, so don't wrap it.
        """
        if wacky_apple_photo_serialize:
            lineLength = REALLY_LARGE
        VCardTextBehavior.serialize(obj, buf, lineLength, validate)

registerBehavior(Photo)


def toListOrString(string):
    stringList = stringToTextValues(string)
    if len(stringList) == 1:
        return stringList[0]
    else:
        return stringList


def splitFields(string):
    """
    Return a list of strings or lists from a Name or Address.
    """
    return [toListOrString(i) for i in
            stringToTextValues(string, listSeparator=';', charList=';')]


def toList(stringOrList):
    if isinstance(stringOrList, basestring):
        return [stringOrList]
    return stringOrList


def serializeFields(obj, order=None):
    """
    Turn an object's fields into a ';' and ',' seperated string.

    If order is None, obj should be a list, backslash escape each field and
    return a ';' separated string.
    """
    fields = []
    if order is None:
        fields = [backslashEscape(val) for val in obj]
    else:
        for field in order:
            escapedValueList = [backslashEscape(val) for val in
                                toList(getattr(obj, field))]
            fields.append(','.join(escapedValueList))
    return ';'.join(fields)


NAME_ORDER = ('family', 'given', 'additional', 'prefix', 'suffix')
ADDRESS_ORDER = ('box', 'extended', 'street', 'city', 'region', 'code',
                 'country')


class NameBehavior(VCardBehavior):
    """
    A structured name.
    """
    hasNative = True

    @staticmethod
    def transformToNative(obj):
        """
        Turn obj.value into a Name.
        """
        if obj.isNative:
            return obj
        obj.isNative = True
        obj.value = Name(**dict(zip(NAME_ORDER, splitFields(obj.value))))
        return obj

    @staticmethod
    def transformFromNative(obj):
        """
        Replace the Name in obj.value with a string.
        """
        obj.isNative = False
        obj.value = serializeFields(obj.value, NAME_ORDER)
        return obj
registerBehavior(NameBehavior, 'N')


class AddressBehavior(VCardBehavior):
    """
    A structured address.
    """
    hasNative = True

    @staticmethod
    def transformToNative(obj):
        """
        Turn obj.value into an Address.
        """
        if obj.isNative:
            return obj
        obj.isNative = True
        obj.value = Address(**dict(zip(ADDRESS_ORDER, splitFields(obj.value))))
        return obj

    @staticmethod
    def transformFromNative(obj):
        """
        Replace the Address in obj.value with a string.
        """
        obj.isNative = False
        obj.value = serializeFields(obj.value, ADDRESS_ORDER)
        return obj
registerBehavior(AddressBehavior, 'ADR')


class OrgBehavior(VCardBehavior):
    """
    A list of organization values and sub-organization values.
    """
    hasNative = True

    @staticmethod
    def transformToNative(obj):
        """
        Turn obj.value into a list.
        """
        if obj.isNative:
            return obj
        obj.isNative = True
        obj.value = splitFields(obj.value)
        return obj

    @staticmethod
    def transformFromNative(obj):
        """
        Replace the list in obj.value with a string.
        """
        if not obj.isNative:
            return obj
        obj.isNative = False
        obj.value = serializeFields(obj.value)
        return obj
registerBehavior(OrgBehavior, 'ORG')


# ------------------------ vCard 4.0 (RFC 6350) behaviors ---------------------

# RFC 6868 caret escaping for parameter values: ^n is a newline, ^' is a
# double quote and ^^ is a literal caret.  vCard 3.0 has no such escaping,
# so these helpers are only used by the 4.0 behaviors.
_caret_encoded_re = re.compile(r'\^(.)', re.DOTALL)


def caretEscape(value):
    """
    Escape a parameter value according to RFC 6868.
    """
    return value.replace('^', '^^').replace('\n', '^n').replace('"', "^'")


def caretUnescape(value):
    """
    Reverse RFC 6868 caret escaping, leaving unknown sequences untouched.
    """
    def replace(match):
        char = match.group(1)
        if char in 'nN':
            return '\n'
        elif char == "'":
            return '"'
        elif char == '^':
            return '^'
        return match.group(0)
    return _caret_encoded_re.sub(replace, value)


def decodeCaretParams(line):
    """
    Remove RFC 6868 caret escaping from all of a line's parameter values.
    """
    for key, values in line.params.items():
        line.params[key] = [caretUnescape(value) for value in values]


def encodeCaretParams(line):
    """
    Apply RFC 6868 caret escaping to all of a line's parameter values.
    """
    for key, values in line.params.items():
        line.params[key] = [caretEscape(value) for value in values]


def _raiseOrFail(line, msg, raiseException):
    if raiseException:
        raise ValidateError(msg, getattr(line, 'lineNumber', None))
    return False


class VCard4_0Behavior(behavior.Behavior):
    """
    Base behavior for vCard 4.0 properties.

    Handles RFC 6868 caret escaping of parameter values and validates the
    vCard 4.0 PREF parameter, which replaces vCard 3.0's TYPE=PREF.
    """
    allowGroup = True
    versionString = '4.0'

    @classmethod
    def decode(cls, line):
        if line.encoded:
            decodeCaretParams(line)
            line.encoded = False

    @classmethod
    def encode(cls, line):
        if not line.encoded:
            encodeCaretParams(line)
            line.encoded = True

    @classmethod
    def lineValidate(cls, line, raiseException, complainUnrecognized):
        typeValues = [value.upper() for value in line.params.get('TYPE', [])]
        if 'PREF' in typeValues:
            msg = ("{0}: TYPE=PREF is a vCard 3.0 construct, use the PREF "
                   "parameter in vCard 4.0".format(line.name))
            return _raiseOrFail(line, msg, raiseException)
        for pref in line.params.get('PREF', []):
            try:
                valid = 1 <= int(pref) <= 100
            except (TypeError, ValueError):
                valid = False
            if not valid:
                msg = ("{0}: PREF parameter must be an integer between 1 "
                       "and 100, got {1!r}".format(line.name, pref))
                return _raiseOrFail(line, msg, raiseException)
        return True


class VCard4_0TextBehavior(VCard4_0Behavior):
    """
    vCard 4.0 property whose default VALUE type is TEXT.
    """
    @classmethod
    def decode(cls, line):
        if line.encoded:
            decodeCaretParams(line)
            line.value = stringToTextValues(line.value)[0]
            line.encoded = False

    @classmethod
    def encode(cls, line):
        if not line.encoded:
            encodeCaretParams(line)
            line.value = backslashEscape(line.value)
            line.encoded = True


class VCard4_0URIBehavior(VCard4_0Behavior):
    """
    vCard 4.0 property whose default VALUE type is URI (e.g. TEL, PHOTO).

    URI values are not backslash escaped; an explicit VALUE=TEXT parameter
    switches the value to text escaping.
    """
    defaultValue = 'URI'

    @classmethod
    def valueParam(cls, line):
        return line.params.get('VALUE', [cls.defaultValue])[0].upper()

    @classmethod
    def decode(cls, line):
        if line.encoded:
            decodeCaretParams(line)
            if cls.valueParam(line) == 'TEXT':
                line.value = stringToTextValues(line.value)[0]
            line.encoded = False

    @classmethod
    def encode(cls, line):
        if not line.encoded:
            encodeCaretParams(line)
            if cls.valueParam(line) == 'TEXT':
                line.value = backslashEscape(line.value)
            line.encoded = True


# Complete and partial (reduced/truncated) DATE forms from RFC 6350,
# e.g. 19850412, 1985-04-12, 1985-04, 1985, --04, --0412 and ---12.
_datePatterns = tuple(re.compile(pattern) for pattern in (
    r'^\d{8}$',
    r'^\d{4}-\d{2}-\d{2}$',
    r'^\d{4}-\d{2}$',
    r'^\d{4}$',
    r'^--\d{2}$',
    r'^--\d{4}$',
    r'^---\d{2}$',
))
_dateTimePattern = re.compile(r'^\d{8}T\d{6}(Z|[+-]\d{4})?$')


class VCard4_0DateBehavior(VCard4_0Behavior):
    """
    vCard 4.0 date-and-or-time property (BDAY, ANNIVERSARY).

    Complete dates transform to datetime.date and complete date-times to
    datetime.datetime.  Partial dates (like a birthday without a year,
    "--0412") and explicit VALUE=TEXT values are preserved as strings.
    """
    hasNative = True

    @classmethod
    def _isText(cls, line):
        return line.params.get('VALUE', [''])[0].upper() == 'TEXT'

    @classmethod
    def decode(cls, line):
        if line.encoded:
            decodeCaretParams(line)
            if cls._isText(line):
                line.value = stringToTextValues(line.value)[0]
            line.encoded = False

    @classmethod
    def encode(cls, line):
        if not line.encoded:
            encodeCaretParams(line)
            if cls._isText(line):
                line.value = backslashEscape(line.value)
            line.encoded = True

    @staticmethod
    def transformToNative(obj):
        """
        Turn a complete date or date-time string into a Python object.
        """
        if obj.isNative:
            return obj
        obj.isNative = True
        value = obj.value
        if not isinstance(value, basestring):
            return obj
        if obj.params.get('VALUE', [''])[0].upper() == 'TEXT':
            return obj
        if re.match(r'^\d{8}$', value):
            obj.value = stringToDate(value)
        elif re.match(r'^\d{4}-\d{2}-\d{2}$', value):
            obj.value = datetime.date(*[int(part) for part in
                                        value.split('-')])
        elif _dateTimePattern.match(value):
            obj.value = stringToDateTime(value)
        # partial dates stay strings, they have no Python representation
        return obj

    @staticmethod
    def transformFromNative(obj):
        """
        Replace a date or datetime in obj.value with its string form.
        """
        if not obj.isNative:
            return obj
        obj.isNative = False
        if isinstance(obj.value, datetime.datetime):
            obj.value = dateTimeToString(obj.value)
        elif isinstance(obj.value, datetime.date):
            obj.value = dateToString(obj.value)
        return obj

    @classmethod
    def lineValidate(cls, line, raiseException, complainUnrecognized):
        if not super(VCard4_0DateBehavior, cls).lineValidate(
                line, raiseException, complainUnrecognized):
            return False
        value = line.value
        if isinstance(value, (datetime.date, datetime.datetime)):
            return True
        if cls._isText(line):
            return True
        if isinstance(value, basestring):
            if _dateTimePattern.match(value):
                return True
            for pattern in _datePatterns:
                if pattern.match(value):
                    return True
        msg = ("{0}: {1!r} is not a valid vCard 4.0 date, date-time or "
               "partial date value".format(line.name, value))
        return _raiseOrFail(line, msg, raiseException)


class FN4_0(VCard4_0TextBehavior):
    name = 'FN'
    description = 'Formatted name (vCard 4.0)'
registerBehavior(FN4_0)


class Tel4_0(VCard4_0URIBehavior):
    name = 'TEL'
    description = 'Telephone number, VALUE defaults to URI in vCard 4.0'
registerBehavior(Tel4_0)


class Email4_0(VCard4_0TextBehavior):
    name = 'EMAIL'
    description = 'Email address (vCard 4.0)'
registerBehavior(Email4_0)


class Photo4_0(VCard4_0URIBehavior):
    name = 'PHOTO'
    description = 'Photograph, a URI (often a data URI) in vCard 4.0'
registerBehavior(Photo4_0)


class Related4_0(VCard4_0URIBehavior):
    name = 'RELATED'
    description = 'A related entity, VALUE defaults to URI in vCard 4.0'
registerBehavior(Related4_0)


class Member4_0(VCard4_0URIBehavior):
    name = 'MEMBER'
    description = 'A member of a group vCard, VALUE is URI in vCard 4.0'
registerBehavior(Member4_0)


class Bday4_0(VCard4_0DateBehavior):
    name = 'BDAY'
    description = 'Birthday (vCard 4.0)'
registerBehavior(Bday4_0)


class Anniversary4_0(VCard4_0DateBehavior):
    name = 'ANNIVERSARY'
    description = 'Anniversary, defined in RFC 6350 (vCard 4.0)'
registerBehavior(Anniversary4_0)


class Kind4_0(VCard4_0TextBehavior):
    name = 'KIND'
    description = 'Kind of entity, defined in RFC 6350 (vCard 4.0)'
    validKinds = ('individual', 'group', 'org', 'location')

    @classmethod
    def lineValidate(cls, line, raiseException, complainUnrecognized):
        if not super(Kind4_0, cls).lineValidate(line, raiseException,
                                                complainUnrecognized):
            return False
        value = line.value
        if isinstance(value, basestring):
            lowered = value.lower()
            if lowered in cls.validKinds or lowered.startswith('x-'):
                return True
        msg = "KIND: {0!r} is not a registered vCard 4.0 kind".format(value)
        return _raiseOrFail(line, msg, raiseException)
registerBehavior(Kind4_0)


class VCard4_0(VCard3_0):
    """
    vCard 4.0 behavior, defined in RFC 6350.
    """
    name = 'VCARD'
    description = 'vCard 4.0, defined in rfc6350'
    versionString = '4.0'
    defaultBehavior = VCard4_0TextBehavior
    knownChildren = {
        'VERSION':     (1, 1, None),  # required, auto-generated
        'FN':          (1, None, '4.0'),
        'N':           (0, 1, None),
        'KIND':        (0, 1, '4.0'),
        'PRODID':      (0, 1, None),
        'UID':         (0, 1, None),
        'ADR':         (0, None, None),
        'ORG':         (0, None, None),
        'TEL':         (0, None, '4.0'),
        'EMAIL':       (0, None, '4.0'),
        'PHOTO':       (0, None, '4.0'),
        'BDAY':        (0, 1, '4.0'),
        'ANNIVERSARY': (0, 1, '4.0'),
        'RELATED':     (0, None, '4.0'),
        'MEMBER':      (0, None, '4.0'),
        'CATEGORIES':  (0, None, None)
    }

    @classmethod
    def validate(cls, obj, raiseException=False, complainUnrecognized=False):
        """
        In addition to the standard checks, MEMBER requires KIND:group.
        """
        valid = super(VCard4_0, cls).validate(obj, raiseException,
                                              complainUnrecognized)
        if not valid or not isinstance(obj, Component):
            return valid
        if obj.contents.get('member'):
            kind = obj.getChildValue('kind', 'individual')
            if str(kind).strip().lower() != 'group':
                msg = ("MEMBER: vCard 4.0 only allows MEMBER properties "
                       "when KIND is group")
                if raiseException:
                    raise ValidateError(msg)
                return False
        return True
registerBehavior(VCard4_0)
