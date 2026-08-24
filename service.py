# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Background service: syncs the lights to what Kodi is playing.

Player callbacks run on a Kodi thread, and a Govee command can block on a
socket or an HTTPS round-trip. Doing that work inline would stall playback, so
the callbacks only record what happened and the service loop does the talking.
"""

import os
import sys
import time

import xbmc
import xbmcaddon
import xbmcgui

_ADDON_PATH = xbmcaddon.Addon().getAddonInfo('path')
_LIB_PATH = os.path.join(_ADDON_PATH, 'resources', 'lib')
if _LIB_PATH not in sys.path:
    sys.path.insert(0, _LIB_PATH)

import addon_utils as utils  # noqa: E402 - needs the sys.path setup above

# How often the clock is consulted for a scheduled sequence. The check is
# cheap; firing one writes a file, and this loop runs twice a second.
SEQUENCE_CHECK_SECONDS = 5

# How long the service waits at a stretch while a sequence is paused. A pause
# can now be an hour, and the loop below is the only thing stepping a cycle and
# turning playback into light changes -- so the wait is taken in slices, with
# that work done between them, rather than as one long block.
PAUSE_SLICE_SECONDS = 0.5

# How often a satellite checks whether it is time to copy from its master. The
# interval it actually copies on is a setting; this is only how finely the loop
# looks at the clock.
SATELLITE_CHECK_SECONDS = 30

EVENT_PLAY = 'play'
EVENT_PAUSE = 'pause'
EVENT_STOP = 'stop'

# Content filter, matching the order of the sync_content setting's values.
CONTENT_VIDEO = 0
CONTENT_VIDEO_AND_MUSIC = 1
CONTENT_EVERYTHING = 2

# Kodi's fullscreen video window; used as a fallback when the boolean
# condition is unavailable on a given build.
WINDOW_FULLSCREEN_VIDEO = 12005


class GoveePlayer(xbmc.Player):
    """Records playback transitions for the service loop to act on.

    Deliberately has no __init__ and takes no constructor arguments. Kodi's
    binding declares Player(int playerCore) and parses constructor arguments
    in the base type, before any subclass __init__ runs -- so passing the
    service in fails with "an integer is required" and no subclass frame in
    the traceback. The service is attached after construction instead; see
    `attach`.
    """

    service = None

    def attach(self, service):
        self.service = service
        return self

    def _notify(self, event):
        # Callbacks can fire between construction and attach, and Kodi keeps
        # calling them after the service asks to be torn down.
        if self.service is not None:
            self.service.queue_event(event)

    def onPlayBackStarted(self):
        self._notify(EVENT_PLAY)

    # Kodi 18+ fires this once streams are actually open. Harmless on Krypton,
    # which never calls it; the queue collapses the duplicate on newer builds.
    def onAVStarted(self):
        self._notify(EVENT_PLAY)

    def onPlayBackPaused(self):
        self._notify(EVENT_PAUSE)

    def onPlayBackResumed(self):
        self._notify(EVENT_PLAY)

    def onPlayBackStopped(self):
        self._notify(EVENT_STOP)

    def onPlayBackEnded(self):
        self._notify(EVENT_STOP)


class GoveeService(xbmc.Monitor):
    """Applies scenes in response to playback, on its own thread."""

    def __init__(self):
        xbmc.Monitor.__init__(self)
        self._app = None
        self._pending = None
        self._last_applied = None
        self._we_dimmed = False
        self._last_sequence_check = 0.0
        # Seconds the loop spent inside a sequence pause since the last
        # schedule check, so anything that came due meanwhile still counts.
        self._blocked_for = 0.0
        self._last_satellite_check = 0.0
        self._last_satellite_sync = 0.0
        self.player = GoveePlayer().attach(self)

    # -- lifecycle ---------------------------------------------------------

    @property
    def app(self):
        """Build the session lazily so a bad setting cannot break startup."""
        if self._app is None:
            from paragon_home import ParagonHome
            self._app = ParagonHome()
        return self._app

    def onSettingsChanged(self):
        utils.debug('Settings changed, rebuilding')
        self._app = None

    # -- event intake ------------------------------------------------------

    def queue_event(self, event):
        """Called from Kodi's thread. Must stay cheap and never block."""
        self._pending = event

    # -- decisions ---------------------------------------------------------

    @staticmethod
    def _content_allowed():
        """Whether the currently playing item is in scope for the user."""
        mode = utils.get_int('sync_content', CONTENT_VIDEO)
        if mode == CONTENT_EVERYTHING:
            return True
        player = xbmc.Player()
        try:
            if player.isPlayingVideo():
                return True
            if mode == CONTENT_VIDEO_AND_MUSIC and player.isPlayingAudio():
                return True
        except Exception:
            # isPlaying* can throw while a stream is still opening.
            return False
        return False

    @staticmethod
    def _fullscreen_ok():
        if not utils.get_bool('sync_fullscreen_only', False):
            return True
        try:
            if xbmc.getCondVisibility('VideoPlayer.IsFullscreen'):
                return True
        except Exception:
            pass
        try:
            return xbmcgui.getCurrentWindowId() == WINDOW_FULLSCREEN_VIDEO
        except Exception:
            return True

    def _scene_for(self, event):
        if event == EVENT_PLAY:
            return utils.get_setting('scene_playing', '')
        if event == EVENT_PAUSE:
            return utils.get_setting('scene_paused', '')
        return utils.get_setting('scene_stopped', '')

    # -- handling ----------------------------------------------------------

    def handle(self, event):
        if not utils.get_bool('playback_sync', False):
            return

        if event in (EVENT_PLAY, EVENT_PAUSE):
            # Krypton fires onPlayBackStarted before the stream is open, so
            # give Kodi a moment to be able to answer isPlayingVideo().
            if not self._wait_for_playback():
                return
            if not self._content_allowed() or not self._fullscreen_ok():
                utils.debug('Skipping %s: content or fullscreen filter' % event)
                return
        elif event == EVENT_STOP:
            # Only restore if this service was the thing that changed the
            # lights. Otherwise stopping a stream would turn on lights the
            # user had deliberately left off.
            if not self._we_dimmed:
                utils.debug('Skipping stop: we never applied a playing scene')
                return

        scene_name = self._scene_for(event)
        if not scene_name:
            utils.debug('No scene configured for %s' % event)
            return

        if event == self._last_applied and event != EVENT_PLAY:
            return

        delay = utils.get_int('sync_delay', 0)
        if delay > 0 and event == EVENT_PLAY:
            if self.waitForAbort(min(delay, 30)):
                return

        utils.debug('Applying scene "%s" for %s' % (scene_name, event))
        applied = self.app.apply_scene_by_name(
            scene_name, announce=utils.get_bool('notify_playback', False))

        self._last_applied = event
        if event == EVENT_PLAY and applied:
            self._we_dimmed = True
        elif event == EVENT_STOP:
            self._we_dimmed = False

    def _wait_for_playback(self, attempts=10):
        """Wait up to ~2s for Kodi to report an open stream."""
        player = xbmc.Player()
        for _ in range(attempts):
            try:
                if player.isPlaying():
                    return True
            except Exception:
                pass
            if self.waitForAbort(0.2):
                return False
        return False

    # -- main loop ---------------------------------------------------------

    def _tick(self):
        """The per-pass work that must keep happening during a pause.

        Deliberately not the schedule check: starting a second sequence inside
        the first one's pause would interleave two sets of commands. Anything
        that comes due while we are busy is picked up when the pause ends,
        which is what the grace allowance below is for.
        """
        try:
            if self.app.cycle_due():
                self.app.cycle_step()
        except Exception as exc:
            utils.log('Cycle step failed: %s' % exc, xbmc.LOGERROR)

        event = self._pending
        if event is not None:
            self._pending = None
            try:
                self.handle(event)
            except Exception as exc:
                # A failing light must never take the service down; it would
                # stop reacting to playback for the rest of the Kodi session.
                utils.log('Error handling %s: %s' % (event, exc), xbmc.LOGERROR)
                import traceback
                utils.log(traceback.format_exc(), xbmc.LOGERROR)

    def _pause(self, seconds):
        """Wait out a sequence pause without stopping everything else.

        Same contract as waitForAbort: returns True if Kodi is shutting down,
        so a sequence still stops promptly when it is.
        """
        started = time.time()
        remaining = float(seconds or 0)
        try:
            while remaining > 0:
                slice_length = min(PAUSE_SLICE_SECONDS, remaining)
                if self.waitForAbort(slice_length):
                    return True
                remaining -= slice_length
                self._tick()
            return False
        finally:
            self._blocked_for += time.time() - started

    def _check_satellite(self, now=None):
        """Copy from the master when this box is a satellite and due to.

        Failure is quiet on purpose. A master that is off, or a network that
        is down, must leave the satellite running on what it already has --
        the lights still work, they are just a little out of date.
        """
        if not self.app.satellite_mode:
            return False

        moment = now or time.time()
        if moment - self._last_satellite_check < SATELLITE_CHECK_SECONDS:
            return False
        self._last_satellite_check = moment

        interval = self.app.sync_minutes * 60
        if self._last_satellite_sync and moment - self._last_satellite_sync < interval:
            return False
        self._last_satellite_sync = moment

        copied, problems = self.app.sync_from_master()
        if problems and not copied:
            utils.debug('Satellite: nothing copied (%s)' % problems[0])
        return bool(copied)

    def _check_sequences(self, now=None):
        """Run anything the clock says is due.

        Checked at most once every few seconds rather than on every tick: the
        test itself is cheap, but a sequence that fires writes a file, and this
        loop runs twice a second.

        A sequence can hold pauses, so its waits go through _pause -- which
        keeps cycling and playback alive meanwhile and still returns True the
        moment Kodi is shutting down, so closing Kodi part way through a
        sequence does not wait for it to finish.

        Whatever time those pauses took is handed to the next check as grace.
        A sequence pausing for an hour is an hour this loop spent unable to
        look at the clock, and without that allowance anything due in the
        meantime would be more than the catch-up window late and get skipped
        rather than run.
        """
        moment = now or time.time()
        if moment - self._last_sequence_check < SEQUENCE_CHECK_SECONDS:
            return []
        self._last_sequence_check = moment

        grace, self._blocked_for = self._blocked_for, 0.0
        alive = lambda index, step: not self.abortRequested()
        ran = self.app.run_due_sequences(sleep_func=self._pause,
                                         on_step=alive, grace=grace)
        # Today's rerack is checked in the same pass and for the same reason.
        ran.extend(self.app.run_due_phases(sleep_func=self._pause,
                                           on_step=alive, grace=grace))
        return ran

    def run(self):
        utils.log('Service started')

        startup_delay = utils.get_int('startup_delay', 10)
        if startup_delay > 0 and self.waitForAbort(min(startup_delay, 120)):
            return

        if self.app.satellite_mode:
            # Before discovery, so a refresh works from the master's list of
            # devices rather than whatever this box last found for itself.
            try:
                copied, problems = self.app.sync_from_master()
                self._last_satellite_sync = time.time()
                utils.log('Satellite: startup copy took %d file(s)%s'
                          % (len(copied),
                             '' if not problems else ', %s' % problems[0]))
            except Exception as exc:
                utils.log('Satellite startup copy failed: %s' % exc,
                          xbmc.LOGERROR)

        if utils.get_bool('discover_on_startup', False):
            try:
                devices, warnings = self.app.refresh_devices()
                utils.log('Startup discovery found %d device(s)' % len(devices))
                for warning in warnings:
                    utils.log('Startup discovery: %s' % warning)
            except Exception as exc:
                utils.log('Startup discovery failed: %s' % exc, xbmc.LOGERROR)

        while not self.abortRequested():
            # Cycling scenes are stepped here rather than on a timer of their
            # own, and playback events are handled here rather than in the
            # callback that raised them: this loop already exists, already
            # stops cleanly on abort, and a half-second tick is far finer than
            # any sensible cycle. A failing light must never take the service
            # down, so _tick swallows and logs rather than raising.
            self._tick()

            # Scheduled sequences are checked here for the same reason
            # cycling is: this loop already exists and already stops cleanly.
            try:
                self._check_sequences()
            except Exception as exc:
                utils.log('Sequence schedule check failed: %s' % exc,
                          xbmc.LOGERROR)

            try:
                self._check_satellite()
            except Exception as exc:
                utils.log('Satellite sync failed: %s' % exc, xbmc.LOGERROR)

            if self.waitForAbort(0.5):
                break

        utils.log('Service stopped')


if __name__ == '__main__':
    service = GoveeService()
    try:
        service.run()
    except Exception as error:
        utils.log('Service crashed: %s' % error, xbmc.LOGERROR)
    finally:
        # Kodi can still fire Player callbacks after the loop exits. Detaching
        # turns those into no-ops rather than letting them reach a service
        # that is on its way out.
        if service.player is not None:
            service.player.service = None
        service.player = None
        del service
        # `time` is imported for this: give Kodi a beat to collect the
        # callback objects before the interpreter for this script exits.
        time.sleep(0.1)
