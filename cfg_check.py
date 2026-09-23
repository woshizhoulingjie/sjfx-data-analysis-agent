import config

c = config.Config
for name in ("OUTPUT_DIR", "SCAN_ALLOWED_ROOTS", "MAX_SINGLE_FILE_BYTES",
             "OWNER_ID", "DB_PATH"):
    print(name, "=", repr(getattr(c, name, "<missing>")))
