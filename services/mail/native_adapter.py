"""Optional MSG-to-EML conversion isolated from the main application process."""
from pathlib import Path
import sys


def main():
    import extract_msg
    source=Path(sys.argv[1]);target=Path(sys.argv[2])
    with extract_msg.openMsg(str(source)) as message:
        eml=message.asEmailMessage()
        (target/'message.eml').write_bytes(eml.as_bytes())

if __name__=='__main__':main()
