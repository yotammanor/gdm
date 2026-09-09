"""Minimal example: map a Drive folder and list its files."""

from gdm import GDM, DriveObject


class Asset(DriveObject, collection="Assets"):
    pass


drive = GDM.open(root_folder_id="YOUR_ROOT_FOLDER_ID")

for asset in drive.collection(Asset).list():
    print(asset.name, asset.mime_type)
