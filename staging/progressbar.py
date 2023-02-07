#!/usr/bin/env python2
#
# Python progressbar functions.
#
# Copyright (C) 2014 Sebastian Lackner
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301, USA
#

import fcntl
import os
import signal
import struct
import sys
import termios

def _sig_winch(signum=None, frame=None):
    """Signal handler for SIGWINCH."""
    global _term_width
    try:
        h, w, hp, wp = struct.unpack('HHHH', fcntl.ioctl(sys.stdout.fileno(),
                       termios.TIOCGWINSZ, struct.pack('HHHH', 0, 0, 0, 0)))
        _term_width  = w
    except IOError:
        # ignore 'IOError: [Errno 25] Inappropriate ioctl for device',
        # which can occur when resizing the window while the output is redirected
        pass

try:
    _sig_winch()
    signal.signal(signal.SIGWINCH, _sig_winch)
except IOError:
    _term_width = int(os.environ.get('COLUMNS', 80)) - 1

class ProgressBar(object):
    def __init__(self, desc="", msg=None, current=0, total=100):
        """Initialize a new progress bar with given properties."""
        self.desc    = desc
        self.msg     = msg
        self.current = current
        self.total   = total

    def __enter__(self):
        self.update()
        return self

    def __exit__(self, type, value, traceback):
        if type is not None:
            sys.stdout.write("\r")
            msg = "<interrupted>"
        else:
            msg = None
        self.finish(msg)
        if self.msg is not None:
            sys.stdout.write("\n")

    def update(self, value = None):
        """Redraw the progressbar and optionally update the value."""
        if value is not None:
            self.current = value

        if self.current == 0 or (self.current >= self.total and self.msg is None):
            sys.stdout.write("%s\r" % (' ' * _term_width))
            sys.stdout.flush()
            return

        width = _term_width / 2
        s1 = self.desc.ljust(width - 1, ' ')[:width - 1]

        width = _term_width - width
        if self.current >= self.total:
            s2 = self.msg.ljust(width, ' ')[:width]
        elif width > 2:
            numbars = min(self.current * (width - 2) / self.total, width - 2)
            s2 = "[%s%s]" % ('#' * numbars, '-' * (width - 2 - numbars))
            percent = " %d%% " % min(self.current * 100 / self.total, 100)
            i = (len(s2)-len(percent))/2
            s2 = "%s%s%s" % (s2[:i], percent, s2[i+len(percent):])

        sys.stdout.write("%s %s\r" % (s1, s2))
        sys.stdout.flush()

    def finish(self, msg = None):
        """Finalize the progressbar."""
        if msg is not None:
            self.msg = msg

        self.current = self.total
        self.update()
        sys.stdout.flush()

if __name__ == '__main__':
    import time

    print("")
    with ProgressBar(desc="description") as x:
        for i in xrange(100):
            x.update(i)
            time.sleep(1)
