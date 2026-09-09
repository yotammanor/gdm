"""Collection membership and safety semantics (explicit moves and deletes)."""

from models_campaign_asset import CampaignAsset

from gdm import GDM


def main() -> None:
    client = GDM.open(root_folder_id="YOUR_ROOT_FOLDER_ID")
    assets = client.collection(CampaignAsset)

    # Adopt a file already in the folder — metadata only, no move
    adopted = assets.adopt("EXISTING_FILE_ID")

    # Bring in from another folder — explicit move or shortcut
    moved = assets.adopt("REMOTE_FILE_ID", move=True)
    shortcut = assets.adopt("REMOTE_FILE_ID", shortcut=True)

    # Remove from collection without deleting the Drive file
    archived = assets.detach(adopted.drive_id, to_folder_id="ARCHIVE_FOLDER_ID")

    # Session delete trashes; permanent delete is explicit on the collection
    assets.trash(moved.drive_id)
    assets.delete_permanently("FILE_ID_TO_PURGE")

    del archived, moved, shortcut, adopted


if __name__ == "__main__":
    main()
