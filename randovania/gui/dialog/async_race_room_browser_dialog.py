from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtGui
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QPushButton, QTableWidgetItem
from qasync import asyncSlot

from randovania.gui.dialog.text_prompt_dialog import TextPromptDialog
from randovania.gui.generated.multiplayer_session_browser_dialog_ui import Ui_MultiplayerSessionBrowserDialog
from randovania.gui.lib import async_dialog, common_qt_lib, model_lib
from randovania.gui.lib.qt_network_client import QtNetworkClient, handle_network_errors
from randovania.network_common import error
from randovania.network_common.session_visibility import MultiplayerSessionVisibility

if TYPE_CHECKING:
    from randovania.network_client.network_client import ConnectionState
    from randovania.network_common.async_race_room import AsyncRaceRoomEntry, AsyncRaceRoomListEntry
    from randovania.network_common.multiplayer_session import MultiplayerSessionListEntry


class AsyncRaceRoomBrowserDialog(QDialog, Ui_MultiplayerSessionBrowserDialog):
    sessions: list[AsyncRaceRoomListEntry]
    visible_sessions: list[AsyncRaceRoomListEntry]
    joined_session: AsyncRaceRoomEntry | None = None

    def __init__(self, network_client: QtNetworkClient):
        super().__init__()
        self.setupUi(self)
        common_qt_lib.set_default_window_icon(self)
        self.network_client = network_client

        headers = ["Name", "Game", "Start Date", "End Date", "Password?", "Creator", "Creation Date"]
        self.item_model = QtGui.QStandardItemModel(0, len(headers), self)
        self.item_model.setHorizontalHeaderLabels(headers)

        self.table_widget.setModel(self.item_model)
        self.table_widget.sortByColumn(8, QtCore.Qt.SortOrder.DescendingOrder)

        self.refresh_button = QPushButton("Refresh")
        self.button_box.addButton(self.refresh_button, QDialogButtonBox.ButtonRole.ResetRole)
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setText("Join")

        self.button_box.accepted.connect(self._attempt_join)
        self.button_box.rejected.connect(self.reject)
        self.refresh_button.clicked.connect(self._refresh_slot)

        self.is_member_label.setVisible(False)
        self.is_member_yes_check.setVisible(False)
        self.is_member_no_check.setVisible(False)

        checks = (
            self.has_password_yes_check,
            self.has_password_no_check,
            self.state_visibile_check,
            self.state_hidden_check,
            self.filter_age_check,
        )
        for check in checks:
            check.stateChanged.connect(self.update_list)
        self.filter_name_edit.textEdited.connect(self.update_list)
        self.filter_age_spin.valueChanged.connect(self.update_list)

        table_selection = self._selection_model()
        table_selection.selectionChanged.connect(self.on_selection_changed)
        self.table_widget.doubleClicked.connect(self.on_double_click)

        self.network_client.ConnectionStateUpdated.connect(self.on_server_connection_state_updated)
        self.on_server_connection_state_updated(self.network_client.connection_state)

    def _selection_model(self) -> QtCore.QItemSelectionModel:
        return self.table_widget.selectionModel()

    @handle_network_errors
    async def refresh(self, *, ignore_limit: bool = False):
        self.refresh_button.setEnabled(False)
        try:
            self.sessions = await self.network_client.get_async_race_room_list(ignore_limit)
            self.update_list()
        finally:
            self.refresh_button.setEnabled(True)
        return True

    @asyncSlot()
    async def _refresh_slot(self):
        await self.refresh(ignore_limit=True)

    def on_selection_changed(self):
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            not self._selection_model().selection().empty()
        )

    @property
    def selected_session(self) -> MultiplayerSessionListEntry:
        # Keep the data structures in variables to ensure lifetime lasts the entire function
        selection: QtCore.QItemSelection = self._selection_model().selection()
        assert not selection.isEmpty()
        selection_range: QtCore.QItemSelectionRange = selection.first()
        return selection_range.topLeft().data(Qt.ItemDataRole.UserRole)

    @asyncSlot(QTableWidgetItem)
    async def on_double_click(self, item):
        await self._attempt_join()

    @asyncSlot()
    @handle_network_errors
    async def _attempt_join(self):
        if not self.visible_sessions:
            return

        session = self.selected_session

        try:
            self.joined_session = await self.network_client.get_async_race_room(session.id, None)
        except error.WrongPasswordError:
            password = await TextPromptDialog.prompt(
                title="Enter password",
                description="This room requires a password:",
                is_password=True,
            )
            if password is None:
                return
            try:
                self.joined_session = await self.network_client.get_async_race_room(session.id, password)
            except error.WrongPasswordError:
                return await async_dialog.warning(self, "Incorrect Password", "The password entered was incorrect.")

        if self.joined_session is not None:
            return self.accept()

    def update_list(self) -> None:
        self.item_model.removeRows(0, self.item_model.rowCount())

        name_filter = self.filter_name_edit.text().strip()

        displayed_has_password = set()
        if self.has_password_yes_check.isChecked():
            displayed_has_password.add(True)
        if self.has_password_no_check.isChecked():
            displayed_has_password.add(False)

        displayed_visibilities = set()
        for check, visibility in (
            (self.state_visibile_check, MultiplayerSessionVisibility.VISIBLE),
            (self.state_hidden_check, MultiplayerSessionVisibility.HIDDEN),
        ):
            if check.isChecked():
                displayed_visibilities.add(visibility)

        dont_filter_age = not self.filter_age_check.isChecked()
        now = datetime.datetime.now(tz=datetime.UTC)
        max_session_age = datetime.timedelta(days=self.filter_age_spin.value())

        visible_sessions = [
            session
            for session in self.sessions
            if (
                session.has_password in displayed_has_password
                and session.visibility in displayed_visibilities
                and name_filter.lower() in session.name.lower()
                and (dont_filter_age or (now - session.creation_date) < max_session_age)
            )
        ]
        self.visible_sessions = visible_sessions

        self.item_model.setRowCount(len(visible_sessions))
        for i, session in enumerate(visible_sessions):
            name = QtGui.QStandardItem(session.name)
            game_name = QtGui.QStandardItem(session.game_summary())
            has_password = QtGui.QStandardItem("Yes" if session.has_password else "No")
            start_date = model_lib.create_date_item(session.start_date)
            end_date = model_lib.create_date_item(session.end_date)
            creator = QtGui.QStandardItem(session.creator)
            creation_date = model_lib.create_date_item(session.creation_date)

            name.setData(session, Qt.ItemDataRole.UserRole)
            for col, item in enumerate(
                (
                    name,
                    game_name,
                    start_date,
                    end_date,
                    has_password,
                    creator,
                    creation_date,
                )
            ):
                self.item_model.setItem(i, col, item)
        self.status_label.setText(f"{len(self.sessions)} rooms total, {len(visible_sessions)} displayed.")
        for i in range(9):
            self.table_widget.resizeColumnToContents(i)

    def on_server_connection_state_updated(self, state: ConnectionState):
        self.server_connection_label.setText(f"Server: {state.value}")
