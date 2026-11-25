"""`python.exe -B -m lagdoctor.service`: the capture service, as the service control manager starts it.

Started any other way it does nothing and exits with ERROR_FAILED_SERVICE_CONTROLLER_CONNECT (1063).
"""
import sys

from lagdoctor import setup
from lagdoctor.relay import protocol
from lagdoctor.service import Host
from lagdoctor.winapi import service


def main() -> int:
    exe = setup.BUNDLED if setup.BUNDLED.is_file() else None      # the install's own PresentMon, never another
    return service.run(protocol.SERVICE_NAME, lambda stop: Host(presentmon_exe=exe).serve(stop))


if __name__ == "__main__":
    sys.exit(main())
