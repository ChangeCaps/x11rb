#!/usr/bin/env python

import getopt
import sys
import glob
import re
import string

try:
    opts, args = getopt.getopt(sys.argv[1:], "p:o:i:")
except getopt.GetoptError as err:
    print(err)
    print('Usage: %s [-p path] -i input -o output' % (sys.argv[0]))
    sys.exit(1)

for (opt, arg) in opts:
    if opt == '-p':
        sys.path.insert(1, arg)
    if opt == '-i':
        input_dir = arg
    if opt == '-o':
        output_file = arg
if args:
    print('No further arguments expected')
    sys.exit(1)

_lines = []
_indent_level = 0
def _out(fmt, *args):
    indent = "".join(["    "] * _indent_level)
    _lines.append(indent + (fmt % args))

def _out_indent_incr():
    global _indent_level
    _indent_level += 1

def _out_indent_decr():
    global _indent_level
    _indent_level -= 1

def _name(name):
    assert len(name) == 2, name
    return name[1]

def _lower_snake_name(name):
    name = _name(name)
    name = re.sub('([a-z0-9])([A-Z])', '\\1_\\2', name)
    return name.lower()

def _upper_snake_name(name):
    return _lower_snake_name(name).upper()

rust_type_mapping = {
        'uint32_t': 'u32',
        'uint16_t': 'u16',
        'uint8_t':  'u8',
        'int32_t':  'i32',
        'int16_t':  'i16',
        'int8_t':   'i8',
        'char':     'u8',
}

def _to_rust_type(name):
    if type(name) == tuple:
        if name[0] == 'xcb':
            name = name[1:]
        if len(name) == 1:
            name = name[0]

    if name in rust_type_mapping:
        return rust_type_mapping[name]
    elif name.isupper():
        return _to_rust_identifier(name)
    else:
        return name

def _to_rust_identifier(name):
    name = re.sub('_(.)', lambda pat: pat.group(1).upper(), name.lower())
    return name[0].upper() + name[1:]

def _to_rust_variable(name):
    if name == "type":
        name = "type_"
    return name

# Now the real fun begins

def rs_open(self):
    _out("pub mod %s {", self.namespace.header)
    _out_indent_incr()
    _out("use std::convert::TryFrom;")
    _out("use std::convert::TryInto;")
    _out("use std::io::IoSlice;")
    _out("use std::error::Error;")
    _out("use crate::xcb_ffi::Connection;")
    _out("use crate::xcb_ffi::SequenceNumber;")
    _out("use crate::xcb_ffi::Cookie;")
    _out("use crate::xcb_ffi::CSlice;")
    _out("use crate::xcb_ffi::{GenericEvent, GenericError};")
    _out("#[derive(Default, Debug)]")
    _out("pub struct MyTryError();")
    _out("impl Error for MyTryError {}")
    _out("impl std::fmt::Display for MyTryError {")
    _out_indent_incr()
    _out("fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {")
    _out_indent_incr()
    _out("write!(f, \"My try error that I should replace with something proper eventually\")")
    _out_indent_decr()
    _out("}")
    _out_indent_decr()
    _out("}")
    _out("")
    _out("trait TryParse: Sized {")
    _out_indent_incr()
    _out("fn try_parse(value: &[u8]) -> Result<(Self, &[u8]), MyTryError>;")
    _out_indent_decr()
    _out("}")
    for (size, name) in [(1, "u8"), (1, "i8"), (2, "u16"), (2, "i16"), (4, "u32"), (4, "i32")]:
        _out("impl TryParse for %s {", name)
        _out_indent_incr()
        _out("fn try_parse(value: &[u8]) -> Result<(Self, &[u8]), MyTryError> {")
        _out_indent_incr()
        _out("if value.len() < %s {", size)
        _out_indent_incr()
        _out("Err(MyTryError())")
        _out_indent_decr()
        _out("} else {")
        _out_indent_incr()
        result_bytes = ", ".join(["value[%s]" % i for i in range(size)])
        _out("Ok((%s::from_ne_bytes([%s]), &value[%s..]))", name, result_bytes, size)
        _out_indent_decr()
        _out("}")
        _out_indent_decr()
        _out("}")
        _out_indent_decr()
        _out("}")
    _out("")

def rs_close(self):
    _out_indent_decr()
    _out("}")

enum_sizes = {}
def rs_enum(self, name):
    has_all_upper = any(ename.isupper() and len(ename) > 1 for (ename, value) in self.values)
    if has_all_upper:
        print(name)

    def ename_to_rust(ename):
        if ename[0].isdigit():
            ename = 'M' + ename
        return ename[0].upper() + ename[1:]

    rust_name = _name(name)
    _out("#[derive(Debug)]")
    if has_all_upper:
        _out("#[allow(non_camel_case_types)]")
    _out("pub enum %s {", rust_name)
    _out_indent_incr()
    for (ename, value) in self.values:
        _out("%s,", ename_to_rust(ename))
    _out_indent_decr()
    _out("}")

    highest_value = max((int(value) for (ename, value) in self.values))
    if highest_value < 1 << 8:
        to_type = "u8"
        larger_types = ["u16", "u32"]
    elif highest_value < 1 << 16:
        to_type = "u16"
        larger_types = ["u32"]
    else:
        assert highest_value < 1 << 32
        to_type = "u32"
        larger_types = []

    enum_sizes[rust_name] = to_type
    _out("impl Into<%s> for %s {", to_type, rust_name)
    _out_indent_incr()
    _out("fn into(self) -> %s {", to_type)
    _out_indent_incr()
    _out("match self {")
    _out_indent_incr()
    bits = [ename for (ename, bit) in self.bits]
    for (ename, value) in self.values:
        if ename not in bits:
            _out("%s::%s => %s,", rust_name, ename_to_rust(ename), value)
    for (ename, bit) in self.bits:
        _out("%s::%s => 1 << %s,", rust_name, ename_to_rust(ename), bit)
    _out_indent_decr()
    _out("}")
    _out_indent_decr()
    _out("}")
    _out_indent_decr()
    _out("}")
    for larger_type in larger_types:
        _out("impl Into<%s> for %s {", larger_type, rust_name)
        _out_indent_incr()
        _out("fn into(self) -> %s {", larger_type)
        _out_indent_incr()
        _out("Into::<%s>::into(self) as _", to_type)
        _out_indent_decr()
        _out("}")
        _out_indent_decr()
        _out("}")
    _out("")

def rs_simple(self, name):
    _out("pub type %s = %s;", _name(name), _to_rust_type(self.name))
    _out("")

def emit_opcode(name, extra_name, opcode):
    _out("pub const %s_%s: u8 = %s;", _upper_snake_name(name), extra_name.upper(), opcode)

def mark_length_fields(self):
    # Find length fields
    length_fields = {}
    for field in self.fields:
        if field.type.is_list:
            length_fields[field.type.expr.lenfield_name] = field

    # Mark length fields as not visible and map them to their list
    for field in self.fields:
        if field.field_name in length_fields:
            field.is_length_field_for = length_fields[field.field_name]
            length_fields[field.field_name].has_length_field = field
            field.visible = False

def _emit_parsing_code(fields):
    parts = []
    for field in fields:
        assert field.wire  # I *guess* that non-wire fields just have to be skipped
        if field.visible or hasattr(field, 'is_length_field_for'):
            rust_type = _to_rust_type(field.type.name)
            if field.type.is_list and field.type.nmemb is not None:
                for i in range(field.type.nmemb):
                    _out("let (%s_%s, r) = %s::try_parse(remaining)?;", field.field_name, i, _to_rust_type(field.type.name));
                    _out("remaining = r;")
                _out("let %s = [", field.field_name)
                _out_indent_incr()
                for i in range(field.type.nmemb):
                    _out("%s_%s,", field.field_name, i)
                _out_indent_decr()
                _out("];")
                parts.append(field.field_name)
            elif field.type.is_list:
                _out("let mut %s = Vec::with_capacity(%s.try_into().or(Err(MyTryError()))?);",
                        field.field_name, field.has_length_field.field_name)
                _out("for _ in 0..%s {", field.has_length_field.field_name)
                _out_indent_incr()
                _out("let (v, r) = %s::try_parse(remaining)?;", rust_type)
                _out("%s.push(v);", field.field_name)
                _out("remaining = r;")
                _out_indent_decr()
                _out("}")
                parts.append(field.field_name)
            else:
                _out("let (%s, r) = %s::try_parse(remaining)?;", _to_rust_variable(field.field_name), rust_type)
                _out("remaining = r;")
                if field.visible:
                    parts.append(_to_rust_variable(field.field_name))
        else:
            if field.type.is_pad and field.type.align != 1:
                assert field.type.size * field.type.nmemb == 1
                align = field.type.align
                _out("// Align offset to multiple of %s", align)
                _out("let offset = remaining.as_ptr() as usize - value.as_ptr() as usize;")
                _out("let misalignment = (%s - (offset %% %s)) %% %s;", align, align, align)
                length = "misalignment"
            else:
                length = field.type.size * field.type.nmemb
            _out("remaining = &remaining.get(%s..).ok_or(MyTryError())?;", length)

    return parts

def complex_type(self, name, from_generic_type, extra_name, name_transform=lambda x: x):
    mark_length_fields(self)

    _out("#[derive(Debug)]")
    _out("pub struct %s%s {", name_transform(_name(name)), extra_name)
    _out_indent_incr()
    for field in self.fields:
        if field.visible:
            field_name = _to_rust_variable(field.field_name)
            if field.type.is_list:
                if field.type.nmemb is None:
                    _out("pub %s: Vec<%s>,", field_name, _to_rust_type(field.type.name))
                else:
                    _out("pub %s: [%s; %s],", field_name, _to_rust_type(field.type.name), field.type.nmemb)
            else:
                _out("pub %s: %s,", field_name, _to_rust_type(field.type.name))
    _out_indent_decr()
    _out("}")

    _out("impl TryParse for %s%s {", name_transform(_name(name)), extra_name)
    _out_indent_incr()
    _out("fn try_parse(value: &[u8]) -> Result<(Self, &[u8]), MyTryError> {")
    _out_indent_incr()
    _out("let mut remaining = value;")
    parts = _emit_parsing_code(self.fields)
    _out("let result = %s%s { %s };", name_transform(_name(name)), extra_name, ", ".join(parts))
    _out("Ok((result, remaining))")
    _out_indent_decr()
    _out("}")
    _out_indent_decr()
    _out("}")

    _out("impl TryFrom<CSlice> for %s%s {", name_transform(_name(name)), extra_name)
    _out_indent_incr()
    _out("type Error = Box<dyn Error>;")
    _out("fn try_from(value: CSlice) -> Result<Self, Self::Error> {")
    _out_indent_incr()
    _out("match Self::try_from(&*value) {")
    _out_indent_incr()
    _out("Ok(v) => Ok(v),")
    _out("Err(e) => Err(Box::new(e))")
    _out_indent_decr()
    _out("}")
    _out_indent_decr()
    _out("}")
    _out_indent_decr()
    _out("}")

    if from_generic_type:
        _out("impl TryFrom<%s> for %s%s {", from_generic_type, name_transform(_name(name)), extra_name)
        _out_indent_incr()
        _out("type Error = Box<dyn Error>;")
        _out("fn try_from(value: %s) -> Result<Self, Self::Error> {", from_generic_type)
        _out_indent_incr()
        _out("Self::try_from(Into::<CSlice>::into(value))")
        _out_indent_decr()
        _out("}")
        _out_indent_decr()
        _out("}")

    _out("impl TryFrom<&[u8]> for %s%s {", name_transform(_name(name)), extra_name)
    _out_indent_incr()
    _out("type Error = MyTryError;")
    _out("fn try_from(value: &[u8]) -> Result<Self, Self::Error> {")
    _out_indent_incr()
    _out("Ok(Self::try_parse(value)?.0)")
    _out_indent_decr()
    _out("}")
    _out_indent_decr()
    _out("}")

def rs_struct(self, name):
    has_variable_size_list = any(field.type.is_list and field.type.nmemb is None for field in self.fields)
    complex_type(self, name, False, '', lambda name: _to_rust_identifier(name))

    if has_variable_size_list:
        length = None
        wire_type = "Vec<u8>"
    else:
        length = sum((field.type.size * field.type.nmemb for field in self.fields))
        wire_type = "[u8; %s]" % length

    _out("impl %s {", _to_rust_identifier(_name(name)))
    _out_indent_incr()

    _out("pub fn to_ne_bytes(&self) -> %s {", wire_type)
    _out_indent_incr()

    if has_variable_size_list:
        _out("let mut result = Vec::new();")

    def _emit():
        if not has_variable_size_list or not result_bytes:
            return
        _out("result.extend([")
        _out_indent_incr()
        for result_value in result_bytes:
            _out("%s,", result_value)
        _out_indent_decr()
        _out("].iter());")
        del result_bytes[:]

    result_bytes = []
    for field in self.fields:
        if field.type.is_pad:
            if has_variable_size_list and field.type.align != 1:
                assert field.type.size == 1
                assert field.type.nmemb == 1
                _out("while result.len() %% %s != 0 {", field.type.align)
                _out_indent_incr()
                _out("result.push(0);")
                _out_indent_decr()
                _out("}")
            else:
                assert field.type.align == 1
                assert field.type.size == 1
                for i in range(field.type.nmemb):
                    result_bytes.append("0")
        elif field.type.is_list and field.type.nmemb is None:
            _emit()
            _out("for obj in self.%s.iter() {", field.field_name)
            _out_indent_incr()
            _out("result.extend(obj.to_ne_bytes().iter());")
            _out_indent_decr()
            _out("}")
        else:
            if hasattr(field, "is_length_field_for"):
                _out("let %s = self.%s.len() as %s;", field.field_name, field.is_length_field_for.field_name, _to_rust_type(field.type.name))
                source = field.field_name
            else:
                source = "self.%s" % field.field_name
            _out("let %s_bytes = %s.to_ne_bytes();", field.field_name, source)
            for i in range(field.type.size):
                result_bytes.append("%s_bytes[%d]" % (field.field_name, i))
    _emit()

    if has_variable_size_list:
        _out("result")
    else:
        _out("[")
        _out_indent_incr()
        for result_value in result_bytes:
            _out("%s,", result_value)
        _out_indent_decr()
        _out("]")
    _out_indent_decr()
    _out("}")
    _out_indent_decr()
    _out("}")

    _out("")

def _to_complex_rust_type(field_type, aux_name, modifier):
    if field_type.is_switch:
        return modifier + aux_name
    result = _to_rust_type(field_type.name)
    if field_type.is_list:
        if field_type.nmemb is None:
            result = "%s[%s]" % (modifier, result)
        else:
            result = "%s[%s; %s]" % (modifier, result, field_type.nmemb)
    return result

def rs_union(self, name):
    rust_name = _name(name)
    _out("#[derive(Debug)]")
    _out("pub struct %s(Vec<u8>);", rust_name)

    _out("impl %s {", rust_name)
    _out_indent_incr()
    for field in self.fields:
        result_type = _to_complex_rust_type(field.type, None, '')
        _out("pub fn as_%s(&self) -> %s {", _lower_snake_name(('xcb', field.field_name)), result_type)
        _out_indent_incr()
        _out("fn do_the_parse(value: &[u8]) -> Result<%s, MyTryError> {", result_type)
        _out_indent_incr()
        _out("let mut remaining = value;")
        parts = _emit_parsing_code([field])
        _out("let _ = remaining;")
        assert len(parts) == 1
        _out("Ok(%s)", parts[0])
        _out_indent_decr()
        _out("}")
        _out("do_the_parse(&self.0[..]).unwrap()")
        _out_indent_decr()
        _out("}")
    _out_indent_decr()
    _out("}")

    fixed_length = max((field.type.size * field.type.nmemb for field in self.fields))

    _out("impl TryParse for %s {", rust_name)
    _out_indent_incr()
    _out("fn try_parse(value: &[u8]) -> Result<(Self, &[u8]), MyTryError> {")
    _out_indent_incr()
    _out("let inner = value[..%s].iter().copied().collect();", fixed_length)
    _out("let result = %s(inner);", rust_name)
    _out("Ok((result, &value[%s..]))", fixed_length)
    _out_indent_decr()
    _out("}")
    _out_indent_decr()
    _out("}")

    _out("")

def _generate_aux(name, request, switch, mask_field):
    field_size = switch.type.fields[0].type.size
    assert all(field.type.size == field_size for field in switch.type.fields)
    mask_field.individual_field_size = field_size

    _out("#[derive(Debug, Default)]")
    _out("pub struct %s {", name)
    _out_indent_incr()
    for field in switch.type.fields:
        _out("pub %s: Option<%s>,", field.field_name, _to_rust_type(field.type.name))
    _out_indent_decr()
    _out("}")

    _out("impl %s {", name)
    _out_indent_incr()
    _out("pub fn to_ne_bytes(&self) -> Vec<u8> {")
    _out_indent_incr()
    _out("let mut result = Vec::new();")
    for field in switch.type.fields:
        _out("if let Some(value) = self.%s {", field.field_name)
        _out_indent_incr()
        _out("result.extend(value.to_ne_bytes().iter());")
        _out_indent_decr()
        _out("}")
    _out("result")
    _out_indent_decr()
    _out("}")

    _out("pub fn value_mask(&self) -> %s {", _to_rust_type(mask_field.type.name))
    _out_indent_incr()
    _out("let mut mask = 0;")
    for field in switch.type.fields:
        expr, = field.parent.expr
        assert expr.op == "enumref"
        enum_name = _name(expr.lenfield_type.name)
        _out("if self.%s.is_some() {", field.field_name)
        _out_indent_incr()
        _out("mask |= Into::<%s>::into(Into::<%s>::into(%s::%s));", _to_rust_type(mask_field.type.name), enum_sizes[enum_name], enum_name, expr.lenfield_name)
        _out_indent_decr()
        _out("}")
    _out("mask")
    _out_indent_decr()
    _out("}")
    _out_indent_decr()
    _out("}")

def rs_request(self, name):
    emit_opcode(name, 'REQUEST', self.opcode)

    skip = [
            'QueryTextExtents', # has an <exprfield> odd_length that we do not support currently
            ]
    if name[1] in skip:
        print("skipping complicated request", self, name)
        return

    switches = list(filter(lambda field: field.type.is_switch, self.fields))
    assert len(switches) <= 1
    if switches:
        aux_name = "%sAux" % _name(name)
        switch = switches[0]

        # Find the mask field for the switch
        lenfield_name = switch.type.expr.lenfield_name
        mask_field = list(filter(lambda field: field.field_name == lenfield_name, self.fields))
        assert len(mask_field) == 1
        mask_field = mask_field[0]

        # Hide it from the API and "connect" it to the switch
        mask_field.visible = False
        mask_field.lenfield_for_switch = switch

        _generate_aux(aux_name, self, switch, mask_field)
    else:
        aux_name = None

    mark_length_fields(self)

    need_lifetime = any(field.visible and field.type.is_list for field in self.fields)
    if need_lifetime:
        generics = ["'c"]
        args = ["c: &'c Connection"]
    else:
        generics = []
        args = ["c: &Connection"]
    where = []

    letters = iter(string.ascii_uppercase)

    for field in self.fields:
        if field.visible:
            rust_type = _to_complex_rust_type(field.type, aux_name, '&')
            if field.enum is not None and not field.type.is_list:
                letter = next(letters)
                generics.append(letter)
                where.append("%s: Into<%s>" % (letter, rust_type))
                rust_type = letter
            args.append("%s: %s" % (_to_rust_variable(field.field_name), rust_type))

    if self.reply:
        if need_lifetime:
            result_type = "Cookie<'c, %sReply>" % _name(name)
        else:
            result_type = "Cookie<%sReply>" % _name(name)
    else:
        result_type = "SequenceNumber"

    if generics:
        lifetime = "<%s>" % ", ".join(generics)
    else:
        lifetime = ""

    _out("pub fn %s%s(%s) -> Result<%s, Box<dyn Error>>", _lower_snake_name(name), lifetime, ", ".join(args), result_type)
    if where:
        _out("where %s", ", ".join(where))
    _out("{")
    _out_indent_incr()

    requests = []
    request = []

    def _emit_request():
        if not request:
            return

        _out("let request%d = [", len(requests));
        requests.append("&request%d" % len(requests))
        _out_indent_incr()
        for byte in request:
            _out("%s,", byte)
        _out_indent_decr()
        _out("];")
        del request[:]

    def _emit_byte_conversion(field_name):
        if field.type.size is not None:
            _out("let mut %s_bytes = Vec::with_capacity(%s * %s.len());", field.field_name, field.type.size, field.field_name);
        else:
            _out("let mut %s_bytes = Vec::new();", field.field_name);
        _out("for value in %s {", field_name);
        _out_indent_incr()
        _out("%s_bytes.extend(value.to_ne_bytes().iter());", field_name)
        _out_indent_decr()
        _out("}")

    fixed_request_length = sum((field.type.size * field.type.nmemb for field in self.fields if field.type.nmemb is not None and field.wire))
    request_length = [ str(fixed_request_length) ]
    for field in self.fields:
        if field.type.nmemb is None:
            size = field.type.size
            if size is None:
                _emit_byte_conversion(field.field_name)
                request_length.append("%s_bytes.len()" % field.field_name)
            else:
                request_length.append("%s * %s.len()" % (size, field.field_name))
        if hasattr(field, 'lenfield_for_switch'):
            _out("let %s = %s.value_mask();", field.field_name, field.lenfield_for_switch.field_name)
            request_length.append("(%s * %s.count_ones()) as usize" % (field.individual_field_size, field.field_name))
    request_length = " + ".join(request_length)

    _out("let length: usize = (%s + 3) / 4;", request_length)
    for field in self.fields:
        if field.field_name == "major_opcode":
            request.append("%s_REQUEST" % _upper_snake_name(name))
        elif field.type.is_pad:
            assert field.type.size == 1
            for i in range(field.type.nmemb):
                request.append("0")
        elif field.type.is_list:
            if field.type.size == 1:
                _emit_request()
                requests.append(field.field_name)
            else:
                if field.type.size is not None:
                    _emit_byte_conversion(field.field_name)

                _emit_request()
                requests.append("&%s_bytes" % field.field_name)
        elif field.wire:
            if hasattr(field, "is_length_field_for"):
                _out("let %s: %s = %s.len().try_into().or(Err(MyTryError()))?;", field.field_name, _to_rust_type(field.type.name), field.is_length_field_for.field_name)
            if field.enum is not None:
                _out("let %s = %s.into();", field.field_name, field.field_name);
            _out("let %s_bytes = %s.to_ne_bytes();", field.field_name, _to_rust_variable(field.field_name))
            if field.type.is_switch:
                _emit_request()
                requests.append("&%s_bytes" % field.field_name)
            else:
                for i in range(field.type.size):
                    request.append("%s_bytes[%d]" % (field.field_name, i))

    _emit_request()

    last_field = self.fields[-1]
    if last_field.type.is_list and not last_field.type.fixed_size():
        _out("let padding = &[0; 3][..(4 - (%s.len() %% 4)) %% 4];", last_field.field_name)
        requests.append("&padding")

    total_length = " + ".join(["(*%s).len()" % r for r in requests])
    _out("assert_eq!(%s, (%s + 3) / 4 * 4);", total_length, request_length);

    slices = ", ".join(["IoSlice::new(%s)" % r for r in requests])

    if self.reply:
        _out("c.send_request_with_reply(&[%s])", slices)
    else:
        _out("c.send_request_without_reply(&[%s])", slices)
    _out_indent_decr()
    _out("}")

    if self.reply:
        complex_type(self.reply, name, False, 'Reply')

    _out("")

def rs_eventstruct(self, name):
    print("eventstruct", self, name)
    _out("")

def rs_event(self, name):
    if self.is_ge_event:
        print("skipping GE event", self, name)
        return
    emit_opcode(name, 'Event', self.opcodes[name])
    complex_type(self, name, 'GenericEvent', 'Event')
    _out("")

def rs_error(self, name):
    emit_opcode(name, 'Error', self.opcodes[name])
    complex_type(self, name, 'GenericError', 'Error')
    _out("")

# We must create an "output" dictionary before any xcbgen imports
output = {'open'       : rs_open,
          'close'      : rs_close,
          'simple'     : rs_simple,
          'enum'       : rs_enum,
          'struct'     : rs_struct,
          'union'      : rs_union,
          'request'    : rs_request,
          'eventstruct': rs_eventstruct,
          'event'      : rs_event,
          'error'      : rs_error,
          }

from xcbgen.state import Module
import xcbgen.xtypes as xtypes

names = glob.glob(input_dir + "/*.xml")
names = [input_dir + "/xproto.xml"]
for name in names:
    module = Module(name, None)
    module.register()
    module.resolve()
    module.generate()

with open(output_file, 'w') as target:
    for line in _lines:
        target.write(line.rstrip())
        target.write('\n')
