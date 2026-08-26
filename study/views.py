# study/views.py (add)
import json
import tempfile
from pathlib import Path

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt  # or your existing session-key auth decorator
from django.views.decorators.http import require_POST

from .models import Recording, StudySession
from .recording_ingest import mux_and_lock_cfr


@require_POST
def upload_recording(request):
    session_key = request.POST.get('session_key')
    stream_source = request.POST.get('stream_source')
    raw_file = request.FILES.get('video_raw')
    sidecar = json.loads(request.POST.get('sidecar_json', '{}'))

    if not (session_key and stream_source and raw_file):
        return JsonResponse({'error': 'missing fields'}, status=400)

    session = StudySession.objects.filter(session_key=session_key).first()
    if not session:
        return JsonResponse({'error': 'unknown session'}, status=404)

    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = Path(tmpdir) / f'{stream_source}.h264'
        with open(raw_path, 'wb') as f:
            for chunk in raw_file.chunks():
                f.write(chunk)

        out_path = Path(tmpdir) / f'{stream_source}.mp4'
        avg_fps = sidecar.get('avg_fps') or sidecar.get('target_fps') or 30
        target_fps = sidecar.get('target_fps') or 30

        mux_and_lock_cfr(raw_path, avg_fps, target_fps, out_path)

        recording = Recording.objects.create(
            session=session,
            stream_source=stream_source,
            frame_count=sidecar.get('frame_count', 0),
            avg_fps_client=avg_fps,
            target_fps=target_fps,
        )
        with open(out_path, 'rb') as f:
            recording.video_file.save(f'{session_key}-{stream_source}.mp4', f, save=True)

        # sidecar itself is worth keeping for QA / gaze-alignment
        recording.sidecar_json.save(
            f'{session_key}-{stream_source}-sidecar.json',
            ContentFile(json.dumps(sidecar).encode('utf-8')),
            save=True,
        )

    return JsonResponse({'status': 'ok', 'recording_id': recording.id})