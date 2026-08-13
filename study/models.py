import uuid

from django.db import models


class Participant(models.Model):
    """One row per participant ID (e.g. P01..P30). Re-used across attempts
    if a participant's session gets abandoned and they log back in."""

    participant_code = models.CharField(max_length=20, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["participant_code"]

    def __str__(self):
        return self.participant_code


class StudySession(models.Model):
    """One row per login -> attempt at the study. A participant can have
    more than one session if they were logged back in after an abandoned
    attempt; the admin list makes it easy to see which is which."""

    END_REASON_CHOICES = [
        ("in_progress", "In progress"),
        ("completed", "Completed"),
        ("timeout", "Timed out"),
        ("manual", "Ended by participant"),
        ("abandoned", "Abandoned (no explicit end received)"),
    ]

    participant = models.ForeignKey(
        Participant, related_name="sessions", on_delete=models.CASCADE
    )
    session_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    stimulus_id = models.CharField(max_length=120, blank=True)

    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(
        max_length=20, choices=END_REASON_CHOICES, default="in_progress"
    )

    user_agent = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.participant.participant_code} · {self.started_at:%Y-%m-%d %H:%M} · {self.end_reason}"

    @property
    def duration_seconds(self):
        if not self.ended_at:
            return None
        return (self.ended_at - self.started_at).total_seconds()


class QuestionResponse(models.Model):
    """One row per question the participant answered, including the Paas
    mental-effort rating and both the time the question was presented and
    the time it was answered (as reported by the browser)."""

    session = models.ForeignKey(
        StudySession, related_name="responses", on_delete=models.CASCADE
    )
    module_id = models.CharField(max_length=120)
    section_id = models.CharField(max_length=120)
    question_id = models.CharField(max_length=120)

    answer_value = models.CharField(max_length=500)
    effort_rating = models.PositiveSmallIntegerField(null=True, blank=True)

    presented_at = models.DateTimeField(
        null=True, blank=True, help_text="When the question screen appeared (client clock)."
    )
    answered_at = models.DateTimeField(
        null=True, blank=True, help_text="When the participant submitted the answer (client clock)."
    )
    server_received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["session", "server_received_at"]
        unique_together = ("session", "question_id")

    def __str__(self):
        return f"{self.session.participant.participant_code} · {self.question_id} = {self.answer_value}"


class ActivityEvent(models.Model):
    """Free-form activity log: screen transitions, server hits, recording
    start/stop, fullscreen changes, etc. `detail` holds anything
    event-specific (e.g. which screen, which stream, error messages)."""

    EVENT_TYPES = [
        ("session_started", "Session started (login)"),
        ("session_ended", "Session ended"),
        ("screen_shown", "Screen shown"),
        ("server_hit", "Server endpoint hit"),
        ("webcam_recording_started", "Webcam recording started"),
        ("webcam_recording_stopped", "Webcam recording stopped"),
        ("screen_recording_started", "Screen recording started"),
        ("screen_recording_stopped", "Screen recording stopped"),
        ("fullscreen_entered", "Fullscreen entered"),
        ("fullscreen_exited", "Fullscreen exited"),
        ("other", "Other"),
    ]

    session = models.ForeignKey(
        StudySession, related_name="events", on_delete=models.CASCADE
    )
    event_type = models.CharField(max_length=40, choices=EVENT_TYPES)
    screen_name = models.CharField(max_length=60, blank=True)
    detail = models.JSONField(default=dict, blank=True)

    client_timestamp = models.DateTimeField(
        null=True, blank=True, help_text="Timestamp reported by the browser, if any."
    )
    server_timestamp = models.DateTimeField(auto_now_add=True)
    request_path = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["session", "server_timestamp"]
        indexes = [models.Index(fields=["session", "event_type"])]

    def __str__(self):
        return f"{self.session.participant.participant_code} · {self.event_type} · {self.server_timestamp:%H:%M:%S}"


def recording_chunk_path(instance, filename):
    stream = instance.stream_source
    return (
        f"recording_chunks/{instance.session.participant.participant_code}/"
        f"{instance.session.session_key}/{stream}/{instance.sequence:06d}_{filename}"
    )


class RecordingChunk(models.Model):
    """One row per uploaded MP4 fragment. Chunks are concatenated into a
    single Recording per stream once the session ends (see study.utils)."""

    STREAM_CHOICES = [("webcam", "Webcam"), ("screen", "Screen")]

    session = models.ForeignKey(
        StudySession, related_name="recording_chunks", on_delete=models.CASCADE
    )
    stream_source = models.CharField(max_length=10, choices=STREAM_CHOICES)
    sequence = models.PositiveIntegerField()
    is_last = models.BooleanField(default=False)
    file = models.FileField(upload_to=recording_chunk_path)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["session", "stream_source", "sequence"]
        unique_together = ("session", "stream_source", "sequence")

    def __str__(self):
        return f"{self.session.participant.participant_code} · {self.stream_source} #{self.sequence}"


def recording_path(instance, filename):
    return (
        f"recordings/{instance.session.participant.participant_code}/"
        f"{instance.session.session_key}/{instance.stream_source}.mp4"
    )


class Recording(models.Model):
    """Final assembled recording per stream per session, built by
    concatenating RecordingChunk files in sequence order once the last
    chunk for that stream has arrived."""

    session = models.ForeignKey(
        StudySession, related_name="recordings", on_delete=models.CASCADE
    )
    stream_source = models.CharField(max_length=10, choices=RecordingChunk.STREAM_CHOICES)
    file = models.FileField(upload_to=recording_path, blank=True)
    chunk_count = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finalized_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("session", "stream_source")

    def __str__(self):
        return f"{self.session.participant.participant_code} · {self.stream_source} recording"
