"""Generate small public synthetic CFR/VFR + audio-beat fixtures; no user videos."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess


def generate(destination):
    destination=Path(destination).resolve();destination.mkdir(parents=True,exist_ok=True)
    ffmpeg=shutil.which('ffmpeg')
    if not ffmpeg:raise RuntimeError('FFmpeg is required only for fixture generation.')
    # FFmpeg/fontconfig resolves an installed sans font; nothing is downloaded.
    label="drawtext=font='sans':text='CFR 24fps  frame %{n}  %{pts\\:hms}':fontcolor=white:fontsize=24:x=20:y=24:box=1:boxcolor=black"
    commands=[
        ['-f','lavfi','-i','testsrc2=size=640x360:rate=24:duration=8','-f','lavfi','-i',r'aevalsrc=if(lt(mod(t\,1)\,0.09)\,0.5*sin(2*PI*880*t)\,0):s=48000:d=8','-vf',label,'-c:v','libx264','-preset','fast','-crf','18','-g','48','-pix_fmt','yuv420p','-c:a','aac','-movflags','+faststart',str(destination/'cfr.mp4')],
        ['-i',str(destination/'cfr.mp4'),'-vf',"select='if(lt(t,4),1,not(mod(n,3)))',drawtext=font='sans':text='VFR PTS %{pts\\:hms}':fontcolor=white:fontsize=19:x=20:y=62:box=1:boxcolor=black",'-fps_mode','vfr','-c:v','libx264','-preset','fast','-crf','18','-g','48','-c:a','copy',str(destination/'vfr.mp4')],
        ['-i',str(destination/'cfr.mp4'),'-t','3','-c:v','libvpx-vp9','-deadline','realtime','-cpu-used','8','-c:a','libopus',str(destination/'vp9.webm')],
        ['-i',str(destination/'cfr.mp4'),'-t','2','-c:v','mpeg4','-c:a','pcm_s16le',str(destination/'mpeg4.avi')],
        ['-itsoffset','1.375','-i',str(destination/'cfr.mp4'),'-c','copy','-copyts',str(destination/'offset.mp4')],
        ['-display_rotation:v:0','90','-i',str(destination/'cfr.mp4'),'-c','copy',str(destination/'rotated.mp4')],
        ['-i',str(destination/'cfr.mp4'),'-an','-vf','setpts=floor(N/2)*2/(24*TB)','-frames:v','16','-fps_mode','passthrough','-c:v','ffv1',str(destination/'duplicate-pts.mkv')],
    ]
    for command in commands:subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-n',*command],check=True,timeout=90)
    probe=shutil.which('ffprobe')
    if probe:
        for name in ('cfr','vfr'):
            value=subprocess.check_output([probe,'-v','error','-select_streams','v:0','-show_entries','frame=best_effort_timestamp_time','-of','json',str(destination/(name+'.mp4'))])
            (destination/(name+'-pts.json')).write_bytes(value)
    return destination


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('destination',type=Path)
    generate(parser.parse_args().destination)
