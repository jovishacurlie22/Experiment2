from django.urls import path

from . import views

app_name = "study"

urlpatterns = [
    path("", views.index, name="index"),
    path("api/login/", views.login_view, name="login"),
    path("api/log-event/", views.log_event, name="log_event"),
    path("api/submit-response/", views.submit_response, name="submit_response"),
    path("api/upload-chunk/", views.upload_chunk, name="upload_chunk"),
    path("api/finish-session/", views.finish_session, name="finish_session"),
    path("api/upload-recording/", views.upload_recording, name="upload_recording"),
    path("log-activity-event/", views.log_activity_event, name="log_activity_event"),
]
