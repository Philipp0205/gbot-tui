"""One chat's state: the transcript, the draft, the phase, and pending decisions."""

from . import activity, backend, herdr, icons, roster, tabs, transcript, widgets


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
        self.agent_id = ""
        self.viewer_session = ""
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
        self.avatar_color = bot.get("avatarColor") or bot.get("avatar_color") or ""
        self.avatar_shape = bot.get("avatarShape") or bot.get("avatar_shape") or ""
        self.label = (bot.get("title") or bot.get("tagline") or "").strip()
        self.deciding = False
        self.answered = {}
        self.pending_request = ""
        self.widget_picks = {}
        self.widget_sent = {}
        self.widget_steps = {}
        self.widget_busy = False
        self.widget_entry = ""
        self.widget_phase = "idle"
        self._held_steps = None
        self.choice_wait = False
        self.content_token = ""
        self.content_at = None
        self.digit_buf = ""
        self.digit_at = None
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

    def note(self, phase):
        if phase != self.phase:
            self.phase = phase
            herdr.publish(phase, self.title, self.session_id, self.resume)

    def apply(self, page, replace):
        if page["name"]:
            self.title = page["name"]
            self.resume = herdr.resume_arg(page["name"]) or herdr.resume_arg(page["id"]) or self.resume
            herdr.bind_app(self.title)
        if page.get("agent_id"):
            self.agent_id = page["agent_id"]
        elif page.get("id") and not page.get("viewer_session"):
            # target.id is the agent id when the thread names no other session.
            self.agent_id = page["id"]
        elif "agent_id" in page and page.get("viewer_session"):
            self.agent_id = page.get("agent_id") or ""
        if page.get("viewer_session"):
            self.viewer_session = page["viewer_session"]
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
        self._stamp_choices()
        self.note_content()
        self.touch_rows()
        return fresh

    def _covers(self, local):
        text = transcript.entry_text(local).strip()
        return any(
            transcript.is_user(entry) and not entry.get("local") and transcript.entry_text(entry).strip() == text
            for entry in self.messages
        )

    def _take_local(self, entry):
        if not transcript.is_user(entry):
            return False
        text = transcript.entry_text(entry).strip()
        if not text:
            return False
        for index, local in enumerate(self.messages):
            if local.get("local") and transcript.entry_text(local).strip() == text:
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
            decision = self.answered.get(transcript.request_of(entry))
            if not decision:
                continue
            card = transcript.message_card(entry)
            if card is not None and card.get("status") == "pending":
                transcript.set_card_status(entry, transcript.DECISION_STATUS[decision])

    def _stamp_choices(self):
        """A poll that has not caught up must not wipe a choice just sent."""
        for entry in self.messages:
            value = self.widget_sent.get(entry.get("id"))
            if value and not widgets.widget_answered(entry):
                entry["respondedValue"] = value

    def touch_rows(self):
        self.rows_rev += 1
        self._rows_key = None

    def sync_activity(self):
        """The open chat is what the list shows on the way back.

        A reply on screen is not new. `working` stays running while it arrives.
        """
        entries = [entry for entry in self.messages if isinstance(entry, dict)]
        cursor = self.cursor or transcript.last_id([entry for entry in entries if not entry.get("local")])
        activity.remember_seen(self.target, self.session_id, cursor)
        backend.mark_read_later(self.session_id)
        shown = [entry for entry in entries if not entry.get("local")]
        # An open widget is needs-you on the list even while this chat is idle.
        if self.phase == "blocked" or (self.phase != "working" and widgets.latest_widget(shown)):
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
                "preview": activity.activity_preview(shown, state),
                "latest": max(
                    (e["timestampMs"] for e in shown if isinstance(e.get("timestampMs"), (int, float))),
                    default=0,
                ),
                "token": activity.content_token(shown),
                "since": 0 if self.content_at is None else self.content_at,
            },
        )

    def begin_decision(self, decision):
        """Mark the newest card decided and return the gbot arguments.

        None means this request was already answered, or there is nothing to answer.
        """
        card = transcript.latest_decision(self.messages, self.answered)
        if not card or decision not in transcript.DECISION_STATUS:
            return None
        request_id = card["request_id"]
        self.answered[request_id] = decision
        status = transcript.DECISION_STATUS[decision]
        for entry in self.messages:
            if entry.get("id") == card["entry_id"] or transcript.request_of(entry) == request_id:
                transcript.set_card_status(entry, status)
        self.pending_request = request_id
        self.deciding = True
        self.touch_rows()
        if self.phase == "blocked" and not transcript.pending_approval(self.messages):
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
                if transcript.request_of(entry) == request_id:
                    transcript.set_card_status(entry, "pending")
            self.touch_rows()
            if transcript.pending_approval(self.messages) and self.phase != "blocked":
                self.note("blocked")
        self.sync_activity()

    def choice_agent_id(self):
        """Agent id for the optional hook. A viewer session is never that id."""
        if self.agent_id:
            return self.agent_id
        if self.viewer_session:
            return ""
        return self.session_id

    def step_at(self, entry):
        if not isinstance(entry, dict):
            return 0
        return (self.widget_steps.get(entry.get("id")) or {}).get("at", 0)

    def note_content(self):
        """Restart the quiet-turn clock when the transcript text changes."""
        token = activity.content_token(self.messages)
        now = activity.clock()
        if token != self.content_token or self.content_at is None:
            self.content_token = token
            self.content_at = now

    def quiet_for(self):
        """True when the visible text has been unchanged for WORKING_TIMEOUT."""
        now = activity.clock()
        if self.content_at is None:
            self.content_at = now
            return False
        return now - self.content_at >= activity.WORKING_TIMEOUT

    def content_expired(self):
        return self.phase == "working" and self.quiet_for()

    def settle_working(self, phase):
        """Keep a live reply in `working`, and give up when it stops changing."""
        if phase == "done" and activity.entry_in_progress(self.messages):
            phase = "working"
        # Any turn with no new text, including one whose last line is the user's
        # or whose isStreaming flag never clears.
        if phase == "working" and self.quiet_for():
            return "idle"
        if phase in {"idle", "done"} and activity.entry_in_progress(self.messages) and not self.quiet_for():
            return "working"
        return phase

    def expire_working(self):
        """Drop a turn that has had no new text for WORKING_TIMEOUT."""
        if not self.content_expired():
            return False
        self.choice_wait = False
        self.note("idle")
        self.sync_activity()
        return True

    def begin_choice(self, value):
        """Mark the newest open widget answered and return the gateway arguments.

        The mark shows immediately. The value is what `gbot send` will carry.
        `agent_id` is empty when the thread only had a session id.
        """
        if self.widget_busy or not self.loaded or not (self.agent_id or self.session_id):
            return None
        entry = widgets.latest_widget(self.messages)
        if entry is None:
            return None
        entry_id = entry.get("id") if isinstance(entry.get("id"), str) else ""
        if not entry_id or not value:
            return None
        self.widget_sent[entry_id] = value
        entry["respondedValue"] = value
        self.widget_picks.pop(entry_id, None)
        self.messages.append({"role": "user", "text": value, "local": True, "widget_answer": entry_id})
        self.widget_entry = entry_id
        self.widget_busy = True
        self.widget_phase = self.phase
        self.choice_wait = True
        self.saw_other = False
        self.quiet = 0
        self.touch_rows()
        self.note_content()
        self.note("working")
        self.sync_activity()
        return (self.target, self.choice_agent_id(), entry_id, value)

    def commit_choice(self, value):
        """Record one answer. A finished card returns the gateway arguments.

        A `steps` card advances one question at a time and sends once, with a
        blank line between answers. "step" means another question is waiting.
        """
        if not value:
            return None
        entry = widgets.latest_widget(self.messages)
        if entry is None:
            return None
        steps = widgets.step_questions(entry)
        if not steps:
            return self.begin_choice(value)
        entry_id = entry.get("id") if isinstance(entry.get("id"), str) else ""
        if not entry_id:
            return None
        state = self.widget_steps.get(entry_id) or {"at": 0, "answers": []}
        answers = list(state.get("answers") or [])
        at = int(state.get("at") or 0)
        if at >= len(steps):
            return None
        answers.append(value)
        at += 1
        self.widget_picks.pop(entry_id, None)
        if at < len(steps):
            self.widget_steps[entry_id] = {"at": at, "answers": answers}
            self.touch_rows()
            return "step"
        self._held_steps = (entry_id, {"at": len(steps) - 1, "answers": answers[:-1]})
        self.widget_steps.pop(entry_id, None)
        return self.begin_choice("\n\n".join(answers))

    def toggle_choice(self, index):
        entry = widgets.latest_widget(self.messages)
        if entry is None:
            return False
        _prompt, options, multi, _custom = widgets.current_question(entry, self.step_at(entry))
        if not multi:
            return False
        if index < 0 or index >= len(options):
            return False
        entry_id = entry.get("id")
        value = options[index][1]
        picks = list(self.widget_picks.get(entry_id) or [])
        if value in picks:
            picks.remove(value)
        else:
            picks.append(value)
        if picks:
            self.widget_picks[entry_id] = picks
        else:
            self.widget_picks.pop(entry_id, None)
        self.touch_rows()
        return True

    def finish_choice(self):
        """The widget call returned. The next read is the whole thread."""
        self.widget_busy = False
        self.widget_entry = ""
        self.full_reload = True

    def fail_choice(self):
        """The widget call failed. Put the options back so they can be picked again."""
        entry_id = self.widget_entry
        value = self.widget_sent.pop(entry_id, None)
        self.widget_busy = False
        self.widget_entry = ""
        self.choice_wait = False
        self.messages = [
            entry
            for entry in self.messages
            if not (entry.get("local") and entry.get("widget_answer") == entry_id)
        ]
        for entry in self.messages:
            if entry.get("id") == entry_id and entry.get("respondedValue") == value:
                entry.pop("respondedValue", None)
        if self._held_steps and self._held_steps[0] == entry_id:
            self.widget_steps[entry_id] = self._held_steps[1]
        self._held_steps = None
        self.touch_rows()
        self.note_content()
        phase = self.widget_phase if self.widget_phase in {"working", "blocked"} else "idle"
        self.note(phase)
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
            # An older answer is not this turn's reply. Counting it would
            # clear `working` after two quiet polls while the bot is still going.
            if (
                self.phase == "working"
                and not self.choice_wait
                and not activity.awaiting_reply(self.messages)
                and not activity.entry_in_progress(self.messages)
                and any(not transcript.is_user(entry) and transcript.entry_text(entry).strip() for entry in self.messages)
            ):
                self.saw_other = True
            self.choice_wait = False
        else:
            fresh = self.apply(page, replace=False)
        if fresh or replace:
            self.idle_polls = 0
        elif self.phase in {"idle", "done"}:
            self.idle_polls += 1
        if first:
            self.loaded = True
            icons.show_avatar_later(page)
            if transcript.pending_approval(self.messages):
                phase = "blocked"
            elif self.phase == "working" or activity.awaiting_reply(self.messages) or activity.entry_in_progress(self.messages):
                phase = "working"
            else:
                phase = "idle"
            phase = self.settle_working(phase)
            self.phase = phase
            herdr.publish(phase, self.title, self.session_id, self.resume)
            tabs.TAB.show(self.title)
            self.sync_activity()
            return
        phase, self.quiet, self.saw_other = transcript.watch_step(
            self.phase,
            fresh,
            self.quiet,
            self.saw_other,
            transcript.pending_approval(self.messages),
        )
        phase = self.settle_working(phase)
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
        self.note_content()
        self.note("working")
        self.sync_activity()
        return text
