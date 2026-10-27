import os as _os
REPO = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", ".."))
import sys, types, logging
def mod(name, **kw):
    m = types.ModuleType(name); m.__dict__.update(kw); sys.modules[name] = m; return m
for n in ["hailo","hailo_apps","hailo_apps.python","hailo_apps.python.core","hailo_apps.python.core.gstreamer",
          "hailo_apps.python.pipeline_apps","hailo_apps.python.pipeline_apps.detection","hailo_apps.python.core.common"]:
    mod(n)
class _Base:
    def __init__(self,*a,**k): pass
mod("hailo_apps.python.core.gstreamer.gstreamer_app", app_callback_class=_Base, GStreamerApp=_Base)
mod("hailo_apps.python.pipeline_apps.detection.detection_pipeline", GStreamerDetectionApp=_Base)
mod("hailo_apps.python.core.common.buffer_utils", get_caps_from_pad=None, get_numpy_from_buffer=None)
mod("hailo_apps.python.core.common.hailo_logger", get_logger=logging.getLogger)
mod("hailo_apps.python.core.common.core", resolve_hef_path=lambda *a,**k: "")
mod("fast_plate_ocr", LicensePlateRecognizer=_Base)
sys.path.insert(0, REPO + "/pi")
import lpr
