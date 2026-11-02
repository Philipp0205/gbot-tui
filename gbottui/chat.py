"""One chat's state: the transcript, the draft, the phase, and pending decisions."""

from . import activity, herdr, icons, roster, tabs
from .transcript import (
    DECISION_STATUS,
    entry_text,
    has_reply,
    is_user,
    last_id,
    latest_decision,
    message_card,
    pending_approval,
    request_of,
    set_card_status,
    watch_step,
)

# Poll cadence in seconds. Each read is one gbot process, so idle backs off.
WAITING_DELAY = 0.3
STREAM_DELAY = 1.0
SETTLE_DELAY = 2.0
BLOCKED_DELAY = 1.0
IDLE_DELAYS = (2.0, 2.0, 3.0, 5.0, 8.0)


class Chat:
    def __init__(self, target):
        self.target = target
        self.title = target
        self.session_id = ""
        self.messages = []
        self.cursor = ""
        self.phase = "idle"
        self.quiet = 0
        self.saw_other = False
        self.scroll = 0
        self.follow = True
        self.typed = ""
        self.error = ""
        self.resume = herdr.resume_arg(target)
        bot = roster.ROSTER.get(target) or {}
        self.avatar_color = icons.bot_color(bot)
        self.avatar_shape = icons.bot_shape(bot)
        self.label = roster.bot_label(bot)
        self.deciding = False
        self.answered = {}
        self.pending_request = ""
        self.full_reload = False
        self.replacing = False
        self.rows_rev = 0
        self.loaded = False
        self.idle_polls = 0
        self.app_seen = herdr.APP

    def show_cached(self, entries):
        """Paint the last thread seen while the first read is still out.

        Phase stays idle, so y/n cannot answer a card from the cache.
        """
        if entries and not self.messages:
            self.messages = list(entries)
            self.touch_rows()

    def take_roster_label(self):
        """A list refresh behind a named open can bring the tagline late. True when it did."""
        if self.label or self.target not in roster.ROSTER:
            return False
        bot = roster.ROSTER.get(self.target) or {}
        label = roster.bot_label(bot)
        if not label:
            return False
        self.label = label
        self.avatar_color = self.avatar_color or bot.get("avatarColor") or ""
        return True

    def poll_delay(self):
        """Seconds until the next thread read.

        Fast while a reply is coming, slower once it settles, so two quiet
        polls still span about four seconds. An idle chat backs off to
        IDLE_DELAYS[-1]; a key press brings it back to the first step.
        """
        if self.phase == "blocked":
            return BLOCKED_DELAY
        if self.phase == "working":
            if not self.saw_other:
                return WAITING_DELAY
            return STREAM_DELAY if self.quiet == 0 else SETTLE_DELAY
        return IDLE_DELAYS[min(self.idle_polls, len(IDLE_DELAYS) - 1)]

    def nudge(self):
        self.idle_polls = 0

    def publish(self):
        herdr.publish(self.phase, self.title, self.session_id, self.resume)

    def note(self, phase):
        if phase != self.phase:
            self.phase = phase
            self.publish()

    def apply(self, page, replace):
        if page["name"]:
            self.title = page["name"]
            self.resume = herdr.resume_arg(page["name"]) or herdr.resume_arg(page["id"]) or self.resume
            herdr.bind_app(self.title)
        if page["id"]:
            self.session_id = page["id"]
        if page.get("avatar_shape"):
            self.avatar_shape = page["avatar_shape"]
        if page.get("avatar_color"):
            self.avatar_color = page["avatar_color"]
        if replace or page["gap_reset"]:
            pending = [entry for entry in self.messages if entry.get("local")]
            self.messages = list(page["entries"])
            for entry in pending:
                if not self._covers(entry):
                    self.messages.append(entry)
            self.cursor = page["cursor"]
            fresh = []
        else:
            fresh = list(page["entries"])
            for entry in fresh:
                self._absorb(entry)
            if page["cursor"]:
                self.cursor = page["cursor"]
        self._stamp_answered()
        self.touch_rows()
        return fresh

    def _covers(self, local):
        text = entry_text(local).strip()
        return any(
            is_user(entry) and not entry.get("local") and entry_text(entry).strip() == text
            for entry in self.messages
        )

    def _take_local(self, entry):
        if not is_user(entry):
            return False
        text = entry_text(entry).strip()
        if not text:
            return False
        for index, local in enumerate(self.messages):
            if local.get("local") and entry_text(local).strip() == text:
                self.messages[index] = entry
                return True
        return False

    def _absorb(self, entry):
        entry_id = entry.get("id") if isinstance(entry, dict) else None
        if isinstance(entry_id, str) and entry_id:
            for index, existing in enumerate(self.messages):
                if existing.get("id") == entry_id:
                    self.messages[index] = entry
                    return
        if self._take_local(entry):
            return
        index = next((i for i, local in enumerate(self.messages) if local.get("local")), None)
        if index is None:
            self.messages.append(entry)
        else:
            self.messages.insert(index, entry)

    def _stamp_answered(self):
        """A poll that still says pending must not undo a decision just sent."""
        for entry in self.messages:
            decision = self.answered.get(request_of(entry))
            if not decision:
                continue
            card = message_card(entry)
            if card is not None and card.get("status") == "pending":
                set_card_status(entry, DECISION_STATUS[decision])

    def touch_rows(self):
        self.rows_rev += 1
        self._rows_key = None

    def sync_activity(self):
        """The open chat is what the list shows on the way back.

        A reply on screen is not new. `working` stays running while it arrives.
        """
        entries = [entry for entry in self.messages if isinstance(entry, dict)]
        cursor = self.cursor or last_id([entry for entry in entries if not entry.get("local")])
        activity.remember_seen(self.target, self.session_id, cursor)
        if self.phase == "blocked":
            state = "blocked"
        elif self.phase == "working":
            state = "working"
        else:
            state = "idle"
        activity.store_activity(
            {"id": self.session_id, "name": self.target},
            {
                "state": state,
                "unread": 0,
                "cursor": cursor,
                "preview": activity.activity_preview(entries, state),
            },
        )

    def begin_decision(self, decision):
        """Mark the newest card decided and return the gbot arguments.

        None means this request was already answered, or there is nothing to answer.
        """
        card = latest_decision(self.messages, self.answered)
        if not card or decision not in DECISION_STATUS:
            return None
        request_id = card["request_id"]
        self.answered[request_id] = decision
        status = DECISION_STATUS[decision]
        for entry in self.messages:
            if entry.get("id") == card["entry_id"] or request_of(entry) == request_id:
                set_card_status(entry, status)
        self.pending_request = request_id
        self.deciding = True
        self.touch_rows()
        if self.phase == "blocked" and not pending_approval(self.messages):
            self.note("idle")
        self.sync_activity()
        return (self.target, card["entry_id"], request_id, decision)

    def finish_decision(self):
        """The respond call returned. The next read is the whole thread."""
        self.deciding = False
        self.pending_request = ""
        self.full_reload = True

    def fail_decision(self):
        """The respond call failed. Put the card back so it can be retried."""
        request_id = self.pending_request
        self.deciding = False
        self.pending_request = ""
        if request_id:
            self.answered.pop(request_id, None)
            for entry in self.messages:
                if request_of(entry) == request_id:
                    set_card_status(entry, "pending")
            self.touch_rows()
            if pending_approval(self.messages) and self.phase != "blocked":
                self.note("blocked")
        self.sync_activity()

    def take_read(self):
        """Arguments for the next `gbot thread` call.

        After a decision the following read drops `--after` once, so the
        gateway can return the card with its new status. Later polls resume
        from the cursor.
        """
        if self.full_reload:
            self.full_reload = False
            self.replacing = True
            return (self.target,)
        if self.cursor:
            return (self.target, self.cursor)
        return (self.target,)

    def read_failed(self):
        if self.replacing:
            self.replacing = False
            self.full_reload = True

    def ingest(self, page, first=False):
        replace = first or not self.cursor or page["gap_reset"] or self.replacing
        self.replacing = False
        if replace:
            self.apply(page, replace=True)
            fresh = []
            if self.phase == "working" and has_reply(self.messages):
                self.saw_other = True
        else:
            fresh = self.apply(page, replace=False)
        if fresh or replace:
            self.idle_polls = 0
        elif self.phase in {"idle", "done"}:
            self.idle_polls += 1
        if first:
            self.loaded = True
            icons.show_avatar_later(page)
            if pending_approval(self.messages):
                self.phase = "blocked"
            elif self.phase != "working":
                self.phase = "idle"
            self.publish()
            tabs.TAB.show(self.title)
            self.sync_activity()
            return
        phase, self.quiet, self.saw_other = watch_step(
            self.phase,
            fresh,
            self.quiet,
            self.saw_other,
            pending_approval(self.messages),
        )
        self.note(phase)
        self.sync_activity()

    def arm_send(self):
        text = self.typed.strip()
        if not text:
            return ""
        self.typed = ""
        self.error = ""
        self.saw_other = False
        self.quiet = 0
        self.follow = True
        self.messages.append({"role": "user", "text": text, "local": True})
        self.note("working")
        self.sync_activity()
        return text
