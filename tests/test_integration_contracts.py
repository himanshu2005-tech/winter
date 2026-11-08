# -*- coding: utf-8 -*-
"""External integrations keep a complete, executable outcome surface."""

import base64
from pathlib import Path
from unittest import mock

import action_bus
import actions  # registers dispatcher handlers
import capabilities
import integration_contracts
from providers import google_api


def test_every_promised_integration_operation_is_executable():
    available = action_bus.BUS.names() | set(capabilities.CAPABILITIES)
    assert integration_contracts.missing(available) == {}


def test_every_promised_integration_operation_has_schema_and_authorization():
    import action_schema
    import autonomy

    promised = integration_contracts.promised_names()
    assert promised <= set(action_schema.ACTION_SPECS)
    assert promised <= set(autonomy.KNOWN_ACTIONS)


@mock.patch("providers.google_api.get_credentials", return_value=object())
@mock.patch("providers.google_api.build")
def test_gmail_attachment_download_uses_exact_ids_and_writes_artifact(
        build, _credentials, tmp_path):
    service = mock.MagicMock()
    build.return_value = service
    messages = service.users.return_value.messages.return_value
    messages.get.return_value.execute.return_value = {
        "payload": {"parts": [{
            "filename": "brief.pdf", "mimeType": "application/pdf",
            "body": {"attachmentId": "att-7", "size": 4},
        }]}}
    messages.attachments.return_value.get.return_value.execute.return_value = {
        "data": base64.urlsafe_b64encode(b"PDF!").decode("ascii")}

    result = google_api.download_email_attachment(
        str(tmp_path), "message-3", attachment_id="att-7")

    assert result.startswith("[Success]")
    saved = tmp_path / "vault" / "inbox" / "brief.pdf"
    assert saved.read_bytes() == b"PDF!"
    messages.attachments.return_value.get.assert_called_once_with(
        userId="me", messageId="message-3", id="att-7")


def test_task_delete_calls_exact_task_and_list():
    service = mock.MagicMock()
    service.tasklists.return_value.list.return_value.execute.return_value = {
        "items": [{"id": "list-2", "title": "College"}]}
    with mock.patch("providers.google_api._tasks_service", return_value=service):
        result = google_api.delete_google_task(
            ".", "task-9", tasklist="College")
    service.tasks.return_value.delete.assert_called_once_with(
        tasklist="list-2", task="task-9")
    assert result.startswith("[Success]")
