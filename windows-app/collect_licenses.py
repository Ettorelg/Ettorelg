"""Include third-party notices in the distributable."""
from importlib.metadata import distributions
from pathlib import Path
import shutil
import sys

target = Path('dist/AlphaMenu/Licenses')
target.mkdir(parents=True, exist_ok=True)
for distribution in distributions():
    for file in distribution.files or []:
        if 'license' in file.name.lower() or file.name.lower().startswith('copying'):
            source = Path(distribution.locate_file(file))
            if source.is_file():
                folder = target / distribution.metadata['Name']
                folder.mkdir(exist_ok=True)
                shutil.copy2(source, folder / file.name)
python_license = Path(sys.base_prefix) / 'LICENSE.txt'
if python_license.is_file():
    shutil.copy2(python_license, target / 'Python-LICENSE.txt')
