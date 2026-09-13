from services.mail.source import OptionalAdapter

MSGAdapter = lambda: OptionalAdapter("msg", {".msg"}, "extract-msg")
