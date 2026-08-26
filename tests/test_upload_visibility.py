from __future__ import annotations

import unittest

from backend.app import _hide_test_survey_uploads


class UploadVisibilityTests(unittest.TestCase):
    def test_hides_local_update_and_test_survey_uploads(self) -> None:
        uploads = [
            {
                "file_name": "test-local.csv",
                "local_path": "/pipeline/files/local_update/test-local.csv",
                "sharepoint_path": "Documents/exports/test-local.csv",
            },
            {
                "file_name": "test-remote.csv",
                "local_path": "/pipeline/files/other/test-remote.csv",
                "sharepoint_path": "root:/ALP/ALP Metrics/3. Portal Pipeline/test_survey/test-remote.csv",
            },
            {
                "file_name": "production.csv",
                "local_path": "/pipeline/files/pipeline/production.csv",
                "sharepoint_path": "root:/ALP/ALP Metrics/3. Portal Pipeline/pipeline/production.csv",
            },
        ]

        visible = _hide_test_survey_uploads(uploads)

        self.assertEqual([upload["file_name"] for upload in visible], ["production.csv"])

    def test_folder_matching_is_case_insensitive_and_segment_based(self) -> None:
        uploads = [
            {
                "file_name": "hidden.csv",
                "local_path": r"C:\\pipeline\\files\\LOCAL_UPDATE\\hidden.csv",
                "sharepoint_path": "",
            },
            {
                "file_name": "visible-local_update-summary.csv",
                "local_path": "/pipeline/files/pipeline/visible-local_update-summary.csv",
                "sharepoint_path": "",
            },
        ]

        visible = _hide_test_survey_uploads(uploads)

        self.assertEqual([upload["file_name"] for upload in visible], ["visible-local_update-summary.csv"])


if __name__ == "__main__":
    unittest.main()
