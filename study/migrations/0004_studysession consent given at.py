from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("study", "0003_alter_questionresponse_answer_value"),
    ]

    operations = [
        migrations.AddField(
            model_name="studysession",
            name="consent_given_at",
            field=models.DateTimeField(
                null=True,
                blank=True,
                help_text=(
                    "Client-reported timestamp of when the participant clicked "
                    "'Agree and Continue' on the consent screen (client clock)."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="activityevent",
            name="event_type",
            field=models.CharField(
                max_length=32,
                choices=[
                    ("login_complete", "Login Complete"),
                    ("consent_given", "Consent Given"),
                    ("module_start", "Module Start"),
                    ("module_end", "Module End"),
                    ("recording_start", "Recording Start"),
                    ("recording_stop", "Recording Stop"),
                    ("question_shown", "Question Shown"),
                    ("question_answered", "Question Answered"),
                    ("session_end", "Session End"),
                    ("session_started", "Session Started"),
                    ("screen_shown", "Screen Shown"),
                    ("server_hit", "Server Hit"),
                    ("other", "Other"),
                ],
            ),
        ),
    ]