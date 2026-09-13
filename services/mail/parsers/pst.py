from services.mail.source import OptionalAdapter

PSTAdapter = lambda: OptionalAdapter("pst", {".pst"}, "libpff/pypff")
