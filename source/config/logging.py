import logging
import re


class RedactShareLinks(logging.Filter):
    """Keep bearer link secrets out of the development access log."""
    def filter(self, record):
        record.msg = re.sub(r'/s/[^\s/?]+', '/s/[redacted]', record.getMessage())
        record.args = ()
        return True
