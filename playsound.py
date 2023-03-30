import logging
from platform import system

system = system()
logger = logging.getLogger(__name__)


class PlaysoundException(Exception):
    pass


def _canonicalizePath(path):
    """
    Support passing in a pathlib.Path-like object by converting to str.
    """
    import sys
    if sys.version_info[0] >= 3:
        return str(path)
    else:
        # On earlier Python versions, str is a byte string, so attempting to
        # convert a unicode string to str will fail. Leave it alone in this case.
        return path


if system == 'Windows':
    from ctypes import create_unicode_buffer, windll
    from time import time

    class mciError(PlaysoundException):
        def __init__(self, code, command):
            buf = create_unicode_buffer(1024)
            windll.winmm.mciGetErrorStringW(code, buf, len(buf))
            message = u'mciError({}) {}'.format(code, buf.value)
            PlaysoundException.__init__(self, message, command)

    def mci_send_string(fmt, *args):
        command = (u'' + fmt).format(*args)
        # logger.debug(command)
        buf = create_unicode_buffer(32)
        error = windll.winmm.mciSendStringW(command, buf, len(buf))
        if error:
            raise mciError(error, command)
        number = int(buf.value or 0)
        # logger.debug(number)
        return number

    class PlaysoundWin:
        opend = set()
        alias_id = 0

        @classmethod
        def play(cls, sound, block=True):
            sound = _canonicalizePath(sound)

            alias = str(cls.alias_id)
            cls.alias_id += 1

            try:
                mci_send_string('open "{}" alias {}', sound, alias)
            except mciError:
                raise PlaysoundException('could not open {}'.format(sound))

            if block:
                mci_send_string('play {} wait', alias)
                mci_send_string('close {}', alias)
            else:
                mci_send_string('set {} time format milliseconds', alias)
                duration = mci_send_string('status {} length', alias) / 1000
                mci_send_string('play {}', alias)
                expiry = time() + duration
                cls.opend.add((expiry, alias))

            cls.cleanup()

        @classmethod
        def cleanup(cls):
            now = time()
            # Check for expired entries
            expired = [
                entry for entry in cls.opend
                if entry[0] < now
            ]
            # Close expired entries
            for entry in expired:
                mci_send_string('close {}', entry[1])
                cls.opend.discard(entry)


def _handlePathOSX(sound):
    sound = _canonicalizePath(sound)

    if '://' not in sound:
        if not sound.startswith('/'):
            from os import getcwd
            sound = getcwd() + '/' + sound
        sound = 'file://' + sound

    try:
        # Don't double-encode it.
        sound.encode('ascii')
        return sound.replace(' ', '%20')
    except UnicodeEncodeError:
        try:
            from urllib.parse import quote  # Try the Python 3 import first...
        except ImportError:
            # Try using the Python 2 import before giving up entirely...
            from urllib import quote

        parts = sound.split('://', 1)
        return parts[0] + '://' + quote(parts[1].encode('utf-8')).replace(' ', '%20')


def _playsoundOSX(sound, block=True):
    '''
    Utilizes AppKit.NSSound. Tested and known to work with MP3 and WAVE on
    OS X 10.11 with Python 2.7. Probably works with anything QuickTime supports.
    Probably works on OS X 10.5 and newer. Probably works with all versions of
    Python.

    Inspired by (but not copied from) Aaron's Stack Overflow answer here:
    http://stackoverflow.com/a/34568298/901641

    I never would have tried using AppKit.NSSound without seeing his code.
    '''
    try:
        from AppKit import NSSound
    except ImportError:
        logger.warning(
            "playsound could not find a copy of AppKit - falling back to using macOS's system copy.")
        sys.path.append(
            '/System/Library/Frameworks/Python.framework/Versions/2.7/Extras/lib/python/PyObjC')
        from AppKit import NSSound

    from time import sleep

    from Foundation import NSURL

    sound = _handlePathOSX(sound)
    url = NSURL.URLWithString_(sound)
    if not url:
        raise PlaysoundException('Cannot find a sound with filename: ' + sound)

    for i in range(5):
        nssound = NSSound.alloc().initWithContentsOfURL_byReference_(url, True)
        if nssound:
            break
        else:
            logger.debug(
                'Failed to load sound, although url was good... ' + sound)
    else:
        raise PlaysoundException(
            'Could not load sound with filename, although URL was good... ' + sound)
    nssound.play()

    if block:
        sleep(nssound.duration())


def _playsoundNix(sound, block=True):
    """Play a sound using GStreamer.

    Inspired by this:
    https://gstreamer.freedesktop.org/documentation/tutorials/playback/playbin-usage.html
    """
    sound = _canonicalizePath(sound)

    # pathname2url escapes non-URL-safe characters
    from os.path import abspath, exists
    try:
        from urllib.request import pathname2url
    except ImportError:
        # python 2
        from urllib import pathname2url

    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst

    Gst.init(None)

    playbin = Gst.ElementFactory.make('playbin', 'playbin')
    if sound.startswith(('http://', 'https://')):
        playbin.props.uri = sound
    else:
        path = abspath(sound)
        if not exists(path):
            raise PlaysoundException(u'File not found: {}'.format(path))
        playbin.props.uri = 'file://' + pathname2url(path)

    set_result = playbin.set_state(Gst.State.PLAYING)
    if set_result != Gst.StateChangeReturn.ASYNC:
        raise PlaysoundException(
            "playbin.set_state returned " + repr(set_result))

    # FIXME: use some other bus method than poll() with block=False
    # https://lazka.github.io/pgi-docs/#Gst-1.0/classes/Bus.html
    logger.debug('Starting play')
    if block:
        bus = playbin.get_bus()
        try:
            bus.poll(Gst.MessageType.EOS, Gst.CLOCK_TIME_NONE)
        finally:
            playbin.set_state(Gst.State.NULL)

    logger.debug('Finishing play')


def _playsoundAnotherPython(otherPython, sound, block=True, macOS=False):
    '''
    Mostly written so that when this is run on python3 on macOS, it can invoke
    python2 on macOS... but maybe this idea could be useful on linux, too.
    '''
    from inspect import getsourcefile
    from os.path import abspath, exists
    from subprocess import check_call
    from threading import Thread

    sound = _canonicalizePath(sound)

    class PropogatingThread(Thread):
        def run(self):
            self.exc = None
            try:
                self.ret = self._target(*self._args, **self._kwargs)
            except BaseException as e:
                self.exc = e

        def join(self, timeout=None):
            super().join(timeout)
            if self.exc:
                raise self.exc
            return self.ret

    # Check if the file exists...
    if not exists(abspath(sound)):
        raise PlaysoundException('Cannot find a sound with filename: ' + sound)

    playsoundPath = abspath(getsourcefile(lambda: 0))
    t = PropogatingThread(target=lambda: check_call(
        [otherPython, playsoundPath, _handlePathOSX(sound) if macOS else sound]))
    t.start()
    if block:
        t.join()


if system == 'Windows':
    playsound = PlaysoundWin.play
elif system == 'Darwin':
    playsound = _playsoundOSX
    import sys
    if sys.version_info[0] > 2:
        try:
            from AppKit import NSSound
        except ImportError:
            logger.warning(
                "playsound is relying on a python 2 subprocess. Please use `pip3 install PyObjC` if you want playsound to run more efficiently.")

            def playsound(sound, block=True): return _playsoundAnotherPython(
                '/System/Library/Frameworks/Python.framework/Versions/2.7/bin/python', sound, block, macOS=True)
else:
    playsound = _playsoundNix
    # Ensure we don't infinitely recurse trying to get another python instance.
    if __name__ != '__main__':
        try:
            import gi
            gi.require_version('Gst', '1.0')
            from gi.repository import Gst
        except:
            logger.warning(
                "playsound is relying on another python subprocess. Please use `pip install pygobject` if you want playsound to run more efficiently.")

            def playsound(sound, block=True): return _playsoundAnotherPython(
                '/usr/bin/python3', sound, block, macOS=False)

del system

if __name__ == '__main__':
    # block is always True if you choose to run this from the command line.
    from sys import argv
    playsound(argv[1])
