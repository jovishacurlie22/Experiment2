from django.contrib import admin

from .models import (
    ActivityEvent,
    Participant,
    QuestionResponse,
    Recording,
    RecordingChunk,
    StudySession,
)


class QuestionResponseInline(admin.TabularInline):
    model = QuestionResponse
    extra = 0
    fields = ("question_id", "module_id", "section_id", "answer_value", "effort_rating", "presented_at", "answered_at")
    readonly_fields = fields
    can_delete = False
    show_change_link = False


class RecordingInline(admin.TabularInline):
    model = Recording
    extra = 0
    fields = ("stream_source", "file", "chunk_count", "started_at", "finalized_at")
    readonly_fields = fields
    can_delete = False


class ActivityEventInline(admin.TabularInline):
    model = ActivityEvent
    extra = 0
    fields = ("server_timestamp", "event_type", "screen_name", "client_timestamp", "request_path", "detail")
    readonly_fields = fields
    can_delete = False
    ordering = ("server_timestamp",)


@admin.register(Participant)
class ParticipantAdmin(admin.ModelAdmin):
    list_display = ("participant_code", "created_at", "session_count")
    search_fields = ("participant_code",)

    def session_count(self, obj):
        return obj.sessions.count()

    session_count.short_description = "Sessions"


@admin.register(StudySession)
class StudySessionAdmin(admin.ModelAdmin):
    list_display = (
        "participant",
        "session_key",
        "started_at",
        "ended_at",
        "end_reason",
        "response_count",
        "event_count",
        "ip_address",
    )
    list_filter = ("end_reason", "started_at")
    search_fields = ("participant__participant_code", "session_key")
    readonly_fields = ("session_key", "started_at", "user_agent", "ip_address")
    inlines = [QuestionResponseInline, RecordingInline, ActivityEventInline]
    date_hierarchy = "started_at"

    def response_count(self, obj):
        return obj.responses.count()

    response_count.short_description = "Responses"

    def event_count(self, obj):
        return obj.events.count()

    event_count.short_description = "Events"


@admin.register(QuestionResponse)
class QuestionResponseAdmin(admin.ModelAdmin):
    list_display = (
        "session",
        "question_id",
        "module_id",
        "section_id",
        "answer_value",
        "effort_rating",
        "presented_at",
        "answered_at",
    )
    list_filter = ("module_id", "section_id")
    search_fields = ("session__participant__participant_code", "question_id", "answer_value")


@admin.register(ActivityEvent)
class ActivityEventAdmin(admin.ModelAdmin):
    list_display = ("session", "event_type", "screen_name", "server_timestamp", "client_timestamp", "request_path")
    list_filter = ("event_type",)
    search_fields = ("session__participant__participant_code", "request_path")
    date_hierarchy = "server_timestamp"


@admin.register(RecordingChunk)
class RecordingChunkAdmin(admin.ModelAdmin):
    list_display = ("session", "stream_source", "sequence", "is_last", "uploaded_at")
    list_filter = ("stream_source", "is_last")
    search_fields = ("session__participant__participant_code",)


@admin.register(Recording)
class RecordingAdmin(admin.ModelAdmin):
    list_display = ("session", "stream_source", "chunk_count", "started_at", "finalized_at")
    list_filter = ("stream_source",)
    search_fields = ("session__participant__participant_code",)
