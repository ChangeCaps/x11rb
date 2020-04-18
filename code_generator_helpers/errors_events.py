"""
This module generates the code for parsing a GenericEvent or GenericError into a
specific instance of the right event/error.
"""

from .output import Indent
from . import camel_case_to_upper_snake


def _get_errors_from_module(module):
    """Get errors from this module"""
    prefix = module.outer_module.namespace.prefix
    errors = module.outer_module.errors.values()
    errors = filter(lambda x: x[0][:len(prefix)] == prefix, errors)
    # The XML for GLX has a comment saying "fake number"
    errors = filter(lambda x: x[0] != ('xcb', 'Glx', 'Generic'), errors)
    return sorted(errors)


def _get_events_from_module(module):
    """Get events from this module"""
    prefix = module.outer_module.namespace.prefix
    events = module.outer_module.events.values()
    events = filter(lambda x: x[0][:len(prefix)] == prefix, events)
    return sorted(events)


def _get_module_name_prefix(module):
    """Get the prefix that should be used for enum variants from this module."""
    if module.namespace.is_ext:
        mod_name = module.namespace.header
        return mod_name[0].upper() + mod_name[1:]
    else:
        return ""


def _emit_type(out, modules, objects, enum_name, kind, extra_cb=None):
    out("/// Enumeration of all possible X11 %ss.", kind)
    out("#[derive(Debug, Clone)]")
    out("pub enum %s<B: std::fmt::Debug + AsRef<[u8]>> {", enum_name)
    out.indent("Unknown(Generic%s<B>),", enum_name)
    if extra_cb:
        extra_cb()
    for module, mod_objects in zip(modules, objects):
        mod_name = module.namespace.header
        variant = _get_module_name_prefix(module)
        for name, object in mod_objects:
            err_name = name[-1]
            if module.has_feature:
                out.indent("#[cfg(feature = \"%s\")]", module.namespace.header)
            out.indent("%s%s(%s::%s%s),", variant, err_name, mod_name, err_name, enum_name)
    out("}")


def _errors(out, modules):
    errors = [_get_errors_from_module(module) for module in modules]
    xproto_index = next(iter(filter(lambda m: not m[1].namespace.is_ext, enumerate(modules))))[0]

    _emit_type(out, modules, errors, "Error", "error")

    out("impl<B: std::fmt::Debug + AsRef<[u8]>> Error<B> {")
    with Indent(out):
        out("/// Parse a generic X11 error into a concrete error type.")
        out("pub fn parse(")
        out.indent("error: GenericError<B>,")
        out.indent("ext_info_provider: &dyn ExtInfoProvider,")
        out(") -> Result<Self, ParseError> {")
        with Indent(out):
            out("let error_code = error.error_code();")

            out("// Check if this is a core protocol error")
            out("match error_code {")
            for name, err in errors[xproto_index]:
                opcode = camel_case_to_upper_snake(name[-1]) + "_ERROR"
                err_name = name[-1]
                out.indent("xproto::%s => return Ok(Self::%s(error.into())),",
                           opcode, err_name)
            out.indent("_ => {}")
            out("}")

            out("// Find the extension that this error could belong to")
            out("let ext_info = ext_info_provider.get_from_error_code(error_code);")
            out("match ext_info {")
            with Indent(out):
                for module, mod_errors in zip(modules, errors):
                    if not mod_errors or not module.namespace.is_ext:
                        continue
                    mod_name = module.namespace.header
                    variant = _get_module_name_prefix(module)
                    if module.has_feature:
                        out("#[cfg(feature = \"%s\")]", module.namespace.header)
                    out("Some((%s::X11_EXTENSION_NAME, ext_info)) => {", mod_name)
                    with Indent(out):
                        out("match error_code - ext_info.first_error {")
                        for name, error in mod_errors:
                            opcode = camel_case_to_upper_snake(name[-1]) + "_ERROR"
                            err_name = name[-1]
                            out.indent("%s::%s => Ok(Self::%s%s(error.into())),",
                                       mod_name, opcode, variant, err_name)
                        out.indent("_ => Ok(Self::Unknown(error)),")
                        out("}")
                    out("}")
                out("_ => Ok(Self::Unknown(error)),")
            out("}")
        out("}")
        out("")

        out("/// Get the sequence number contained in this X11 error")
        out("pub fn wire_sequence_number(&self) -> u16 {")
        with Indent(out):
            out("match self {")
            out.indent("Error::Unknown(value) => value.raw_sequence_number()" +
                       ".expect(\"Errors should always have a sequence number\"),")
            for module, mod_errors in zip(modules, errors):
                variant = _get_module_name_prefix(module)
                for name, error in mod_errors:
                    err_name = name[-1]
                    if module.has_feature:
                        out.indent("#[cfg(feature = \"%s\")]", module.namespace.header)
                    out.indent("Error::%s%s(value) => value.sequence,", variant, err_name)
            out("}")
        out("}")
        out("")

        out("/// Get the error code of this X11 error")
        out("pub fn error_code(&self) -> u8 {")
        with Indent(out):
            out("match self {")
            out.indent("Error::Unknown(value) => value.error_code(),")
            for module, mod_errors in zip(modules, errors):
                variant = _get_module_name_prefix(module)
                for name, error in mod_errors:
                    err_name = name[-1]
                    if module.has_feature:
                        out.indent("#[cfg(feature = \"%s\")]", module.namespace.header)
                    out.indent("Error::%s%s(value) => value.error_code,", variant, err_name)
            out("}")
        out("}")

        out("/// Get the response type of this X11 error")
        out("///")
        out("/// This is not `pub` because it should always be `0` for errors.")
        out("fn raw_response_type(&self) -> u8 {")
        with Indent(out):
            out("match self {")
            out.indent("Error::Unknown(value) => value.response_type(),")
            for module, mod_errors in zip(modules, errors):
                variant = _get_module_name_prefix(module)
                for name, error in mod_errors:
                    err_name = name[-1]
                    if module.has_feature:
                        out.indent("#[cfg(feature = \"%s\")]", module.namespace.header)
                    out.indent("Error::%s%s(value) => value.response_type,", variant, err_name)
            out("}")
        out("}")
    out("}")


def _events(out, modules):
    events = [_get_events_from_module(module) for module in modules]
    xproto_index = next(iter(filter(lambda m: not m[1].namespace.is_ext, enumerate(modules))))[0]

    _emit_type(out, modules, events, "Event", "event", lambda: out.indent("Error(Error<B>),"))

    out("impl<B: std::fmt::Debug + AsRef<[u8]>> Event<B> {")
    with Indent(out):
        out("/// Parse a generic X11 event into a concrete event type.")
        out("#[allow(clippy::cognitive_complexity)]")
        out("pub fn parse(")
        out.indent("event: GenericEvent<B>,")
        out.indent("ext_info_provider: &dyn ExtInfoProvider,")
        out(") -> Result<Self, ParseError> {")
        with Indent(out):
            out("let event_type = event.response_type();")
            out("// Check if this is a core protocol error or from the generic event extension")
            out("match event_type {")
            with Indent(out):
                out("0 => return Ok(Self::Error(Error::parse(event.try_into()?, ext_info_provider)?)),")
                for name, event in events[xproto_index]:
                    if name == ('xcb', 'GeGeneric'):
                        # This does not really count and is parsed as an extension's event
                        continue
                    opcode = camel_case_to_upper_snake(name[-1]) + "_EVENT"
                    event_name = name[-1]
                    out("xproto::%s => return Ok(Self::%s(event.try_into()?)),",
                        opcode, event_name)
                out("xproto::GE_GENERIC_EVENT => return Self::from_generic_event(event, ext_info_provider),")
                out("_ => {}")
            out("}")

            out("// Find the extension that this event could belong to")
            out("let ext_info = ext_info_provider.get_from_event_code(event_type);")
            out("match ext_info {")
            with Indent(out):
                for module, mod_events in zip(modules, events):
                    # XGE events are handled separately
                    has_normal_events = any(not e[1].is_ge_event for e in mod_events)
                    if not has_normal_events or not module.namespace.is_ext:
                        continue
                    mod_name = module.namespace.header
                    variant = _get_module_name_prefix(module)
                    if module.has_feature:
                        out("#[cfg(feature = \"%s\")]", module.namespace.header)
                    out("Some((%s::X11_EXTENSION_NAME, ext_info)) => {", mod_name)
                    with Indent(out):
                        if module.namespace.header == 'xkb':
                            out("if event_type != ext_info.first_event {")
                            out.indent("return Ok(Self::Unknown(event));")
                            out("}")
                            out("match event.raw_bytes()[1] {")
                        else:
                            out("match event_type - ext_info.first_event {")
                        for name, event in mod_events:
                            if event.is_ge_event:
                                continue
                            opcode = camel_case_to_upper_snake(name[-1]) + "_EVENT"
                            event_name = name[-1]
                            out.indent("%s::%s => Ok(Self::%s%s(event.try_into()?)),",
                                       mod_name, opcode, variant, event_name)
                        out.indent("_ => Ok(Self::Unknown(event)),")
                        out("}")
                    out("}")
                out("_ => Ok(Self::Unknown(event)),")
            out("}")
        out("}")
        out("")

        out("fn from_generic_event(")
        out.indent("event: GenericEvent<B>,")
        out.indent("ext_info_provider: &dyn ExtInfoProvider,")
        out(") -> Result<Self, ParseError> {")
        with Indent(out):
            out("let bytes = event.raw_bytes();")
            out("let ge_event = xproto::GeGenericEvent::try_from(bytes)?;")
            out("let ext_name = ext_info_provider")
            out.indent(".get_from_major_opcode(ge_event.extension)")
            out.indent(".map(|(name, _)| name);")
            out("match ext_name {")
            with Indent(out):
                for module, mod_events in zip(modules, events):
                    has_xge_events = any(e[1].is_ge_event for e in mod_events)
                    if not has_xge_events or not module.namespace.is_ext:
                        continue
                    mod_name = module.namespace.header
                    variant = _get_module_name_prefix(module)
                    if module.has_feature:
                        out("#[cfg(feature = \"%s\")]", module.namespace.header)
                    out("Some(%s::X11_EXTENSION_NAME) => {", mod_name)
                    with Indent(out):
                        out("match ge_event.event_type {")
                        for name, event in mod_events:
                            if not event.is_ge_event:
                                continue
                            opcode = camel_case_to_upper_snake(name[-1]) + "_EVENT"
                            event_name = name[-1]
                            out.indent("%s::%s => Ok(Self::%s%s(event.try_into()?)),",
                                       mod_name, opcode, variant, event_name)
                        out.indent("_ => Ok(Self::Unknown(event)),")
                        out("}")
                    out("}")
                out("_ => Ok(Self::Unknown(event)),")
            out("}")
        out("}")
        out("")

        out("/// Get the sequence number contained in this X11 event")
        out("pub fn wire_sequence_number(&self) -> Option<u16> {")
        with Indent(out):
            out("match self {")
            out.indent("Event::Unknown(value) => value.raw_sequence_number(),")
            out.indent("Event::Error(value) => Some(value.wire_sequence_number()),")
            for module, mod_events in zip(modules, events):
                variant = _get_module_name_prefix(module)
                for name, event in mod_events:
                    err_name = name[-1]
                    if module.has_feature:
                        out.indent("#[cfg(feature = \"%s\")]", module.namespace.header)
                    has_sequence = any(field.field_name == "sequence" for field in event.fields)
                    if has_sequence:
                        out.indent("Event::%s%s(value) => Some(value.sequence),", variant, err_name)
                    else:
                        out.indent("Event::%s%s(_) => None,", variant, err_name)
            out("}")
        out("}")
        out("")

        out("/// Get the raw response type of this X11 event")
        out("///")
        out("/// Response types have seven bits in X11. The eight bit indicates whether")
        out("/// the packet was generated through the `SendEvent` request. This function")
        out("/// returns all eight bits.")
        out("///")
        out("/// See also the `response_type()`, `server_generated()` and `sent_event()` methods.")
        out("pub fn raw_response_type(&self) -> u8 {")
        with Indent(out):
            out("match self {")
            out.indent("Event::Unknown(value) => value.raw_response_type(),")
            out.indent("Event::Error(value) => value.raw_response_type(),")
            for module, mod_events in zip(modules, events):
                variant = _get_module_name_prefix(module)
                for name, event in mod_events:
                    err_name = name[-1]
                    if module.has_feature:
                        out.indent("#[cfg(feature = \"%s\")]", module.namespace.header)
                    out.indent("Event::%s%s(value) => value.response_type,", variant, err_name)
            out("}")
        out("}")
        out("")

        out("/// Get the response type of this X11 event")
        out("pub fn response_type(&self) -> u8 {")
        out.indent("self.raw_response_type() & 0x7f")
        out("}")
        out("")

        out("/// Was this event generated by the X11 server?")
        out("///")
        out("/// If this function returns true, then this event comes from the X11 server.")
        out("/// Otherwise, it was sent from another client via the `SendEvent` request.")
        out("pub fn server_generated(&self) -> bool {")
        out.indent("self.raw_response_type() & 0x80 == 0")
        out("}")
        out("")

        out("/// Was this event generated by another X11 client?")
        out("///")
        out("/// If this function returns true, then this event comes from another client via")
        out("/// the `SendEvent` request. Otherwise, it was generated by the X11 server.")
        out("pub fn sent_event(&self) -> bool {")
        out.indent("self.raw_response_type() & 0x80 != 0")
        out("}")
    out("}")


def generate(out, modules):
    _errors(out, modules)
    _events(out, modules)
