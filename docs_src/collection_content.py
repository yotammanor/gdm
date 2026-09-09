"""Native Docs/Slides content handles stay lazy until accessed."""

from models_campaign_asset import CampaignAsset

from gdm import GDM


def main() -> None:
    client = GDM.open(root_folder_id="YOUR_ROOT_FOLDER_ID")
    assets = client.collection(CampaignAsset)
    brief = assets.get("DOC_FILE_ID")

    # Metadata queries never download bodies; content is explicit
    document = brief.content.google_doc()
    document.batch_update(
        [
            {
                "insertText": {
                    "location": {"index": 1},
                    "text": "Updated through the Docs API\n",
                }
            }
        ]
    )

    pdf_bytes = brief.content.export("application/pdf")
    print(len(pdf_bytes))


if __name__ == "__main__":
    main()
