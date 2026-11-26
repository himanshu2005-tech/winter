import unittest
from unittest.mock import MagicMock, patch

import action_schema
import actions
import autonomy
from providers import drive_api, people_api


def person(resource="people/c1", name="Asha Rao", phones=("+91 99999 00000",),
           emails=("asha@example.com",), org="Acme", etag="e1"):
    return {"resourceName": resource, "etag": etag,
            "names": [{"displayName": name, "metadata": {"primary": True}}],
            "phoneNumbers": [{"value": p} for p in phones],
            "emailAddresses": [{"value": e} for e in emails],
            "organizations": [{"name": org, "title": "Engineer"}]}


class PeopleProvider(unittest.TestCase):
    def test_summary_is_flat_and_complete(self):
        row = people_api.summarize(person())
        self.assertEqual(row["name"], "Asha Rao")
        self.assertEqual(row["organization"], "Acme")
        self.assertEqual(row["emails"], ["asha@example.com"])

    def test_search_merges_saved_and_other_contacts(self):
        service = MagicMock()
        people = service.people.return_value.searchContacts.return_value.execute
        people.return_value = {"results": [{"person": person()}]}
        other = service.otherContacts.return_value.search.return_value.execute
        other.return_value = {"results": [{"person": person(
            "otherContacts/c9", "Asha R", (), ("asha.r@mail.com",))}]}
        with patch.object(people_api, "_service", return_value=service):
            out = people_api.search_people(".", "asha")
        self.assertEqual(len(out["people"]), 2)
        self.assertIn("auto-saved from mail", out["text"])

    def test_empty_query_is_an_error_not_a_listing_of_everyone(self):
        out = people_api.search_people(".", "  ")
        self.assertIn("[Error]", out["text"])

    def test_resource_names_must_look_like_resource_names(self):
        with self.assertRaises(ValueError):
            people_api._normalize_resource("Asha")
        self.assertEqual(people_api._normalize_resource("c123"), "people/c123")

    def test_update_adds_a_number_without_dropping_existing_ones(self):
        service = MagicMock()
        service.people.return_value.get.return_value.execute.return_value = person()
        update = service.people.return_value.updateContact
        update.return_value.execute.return_value = person()
        with patch.object(people_api, "_service", return_value=service):
            msg = people_api.update_person(".", "people/c1", phone="+91 88888 11111")
        body = update.call_args.kwargs["body"]
        self.assertEqual([p["value"] for p in body["phoneNumbers"]],
                         ["+91 99999 00000", "+91 88888 11111"])
        self.assertEqual(update.call_args.kwargs["updatePersonFields"], "phoneNumbers")
        self.assertEqual(body["etag"], "e1")
        self.assertIn("[Success]", msg)

    def test_update_with_a_number_already_saved_changes_nothing(self):
        service = MagicMock()
        service.people.return_value.get.return_value.execute.return_value = person()
        with patch.object(people_api, "_service", return_value=service):
            msg = people_api.update_person(".", "people/c1", phone="+9199999 00000")
        service.people.return_value.updateContact.assert_not_called()
        self.assertIn("nothing changed", msg)

    def test_update_with_nothing_to_change_is_refused(self):
        service = MagicMock()
        service.people.return_value.get.return_value.execute.return_value = person()
        with patch.object(people_api, "_service", return_value=service):
            with self.assertRaises(ValueError):
                people_api.update_person(".", "people/c1")

    def test_read_only_grant_becomes_a_reauthorize_instruction(self):
        from googleapiclient.errors import HttpError
        resp = MagicMock(status=403)
        error = HttpError(resp, b'{"error": {"message": "Request had insufficient authentication scopes."}}')
        service = MagicMock()
        service.people.return_value.createContact.return_value.execute.side_effect = error
        with patch.object(people_api, "_service", return_value=service):
            with self.assertRaises(people_api.ContactsWriteNotGranted) as caught:
                people_api.create_person(".", "New Person")
        self.assertIn("Reauthorize", str(caught.exception))

    def test_create_requires_a_name(self):
        with self.assertRaises(ValueError):
            people_api.create_person(".", " ")

    def test_birthday_forms(self):
        self.assertEqual(people_api._parse_birthday("1999-03-14"),
                         {"year": 1999, "month": 3, "day": 14})
        self.assertEqual(people_api._parse_birthday("14/03"), {"day": 14, "month": 3})
        self.assertEqual(people_api._parse_birthday("14 March"), {"month": 3, "day": 14})
        self.assertIsNone(people_api._parse_birthday("someday"))


class DriveExtras(unittest.TestCase):
    def test_read_exports_a_google_doc_as_text_and_reports_truncation(self):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "d1", "name": "Notes", "mimeType": "application/vnd.google-apps.document"}
        fake = MagicMock()
        fake.next_chunk.side_effect = lambda: (None, True)

        def downloader(buffer, request, chunksize=None):
            buffer.write(("word " * 1000).encode())
            return fake

        with patch.object(drive_api, "_service", return_value=service), \
                patch.object(drive_api, "MediaIoBaseDownload", side_effect=downloader):
            out = drive_api.read_file(".", "d1", max_chars=500)
        service.files.return_value.export_media.assert_called_once_with(
            fileId="d1", mimeType="text/plain")
        self.assertIn("showing the first 500 of 5,000", out["text"])

    def test_read_refuses_binary_and_folders_without_downloading(self):
        for mime in ("application/pdf", drive_api.FOLDER_MIME):
            service = MagicMock()
            service.files.return_value.get.return_value.execute.return_value = {
                "id": "x", "name": "x", "mimeType": mime}
            with patch.object(drive_api, "_service", return_value=service), \
                    patch.object(drive_api, "MediaIoBaseDownload") as dl:
                out = drive_api.read_file(".", "x")
            dl.assert_not_called()
            self.assertIn("[Error]", out["text"])

    def test_recent_orders_newest_first_and_excludes_trash_and_folders(self):
        service = MagicMock()
        listing = service.files.return_value.list
        listing.return_value.execute.return_value = {"files": [
            {"id": "a", "name": "A", "mimeType": "text/plain", "modifiedTime": "t"}]}
        with patch.object(drive_api, "_service", return_value=service):
            out = drive_api.recent_files(".", 5, 7)
        kwargs = listing.call_args.kwargs
        self.assertEqual(kwargs["orderBy"], "modifiedTime desc")
        self.assertIn("trashed = false", kwargs["q"])
        self.assertIn(drive_api.FOLDER_MIME, kwargs["q"])
        self.assertIn("id=a", out["text"])

    def test_restore_untrashes(self):
        service = MagicMock()
        service.files.return_value.update.return_value.execute.return_value = {
            "id": "z", "name": "Z", "trashed": False}
        with patch.object(drive_api, "_service", return_value=service):
            msg = drive_api.restore_item(".", "z")
        self.assertEqual(service.files.return_value.update.call_args.kwargs["body"],
                         {"trashed": False})
        self.assertIn("Restored Z", msg)


class Wiring(unittest.TestCase):
    NEW = ("DRIVE_READ", "DRIVE_RECENT", "DRIVE_RESTORE", "PEOPLE_SEARCH",
           "PEOPLE_GET", "PEOPLE_CREATE", "PEOPLE_UPDATE", "PEOPLE_DELETE")
    MUTATING = ("DRIVE_RESTORE", "PEOPLE_CREATE", "PEOPLE_UPDATE", "PEOPLE_DELETE")

    def test_every_new_action_is_known_and_schema_checked(self):
        for name in self.NEW:
            self.assertIn(name, autonomy.KNOWN_ACTIONS, name)
            self.assertIn(name, action_schema.ACTION_SPECS, name)

    def test_mutations_carry_receipt_contracts(self):
        for name in self.MUTATING:
            self.assertIn(name, actions.RECEIPT_CONTRACTS, name)

    def test_contact_deletion_needs_confirmation_and_is_never_proactive(self):
        self.assertIn("PEOPLE_DELETE", autonomy.CONFIRMATION_REQUIRED_ACTIONS)
        for name in self.MUTATING:
            self.assertIn(name, autonomy.PROACTIVE_BLOCKED_ACTIONS, name)

    def test_reads_are_not_blocked_proactively_or_confirmed(self):
        for name in ("PEOPLE_SEARCH", "PEOPLE_GET", "DRIVE_READ", "DRIVE_RECENT"):
            self.assertNotIn(name, autonomy.CONFIRMATION_REQUIRED_ACTIONS)


class Scopes(unittest.TestCase):
    def test_contacts_write_scope_requested_for_new_grants(self):
        from providers import google_api
        self.assertIn("https://www.googleapis.com/auth/contacts", google_api.SCOPES)
        self.assertNotIn("https://www.googleapis.com/auth/contacts.readonly",
                         google_api.SCOPES)

    def test_stored_credentials_are_loaded_with_their_issued_scopes(self):
        import inspect
        from providers import google_api
        source = inspect.getsource(google_api.get_credentials)
        self.assertNotIn(", SCOPES)", source.split("flow =")[0])


if __name__ == "__main__":
    unittest.main()
