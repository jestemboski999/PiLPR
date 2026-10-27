"""Jednorazowe zachowanie wyboru CPU i kopii konfiguracji przed wdrożeniem."""
import json
from pathlib import Path
import shutil

root = Path('/home/admin/hailo-apps')
path = root / 'local_resources/lpr_runtime_config.json'
backup = path.with_name(path.name + '.przed_ocr_hailo_20261006')
if not backup.exists():
    shutil.copy2(path, backup)
data = json.loads(path.read_text())
data['ocr_backend'] = 'cpu'
temporary = path.with_name(path.name + '.ocr_tmp')
temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
temporary.replace(path)
print('Konfiguracja: ocr_backend=cpu; kopia:', backup)
for name in ('worker_cpu.json', 'worker_lvl3.json', 'wynik_round.json',
             'wynik_recompiled.json'):
    result = json.loads((root / 'ocr_test_20261006' / name).read_text())
    print(name, {k: v for k, v in result.items() if k != 'predictions'})
