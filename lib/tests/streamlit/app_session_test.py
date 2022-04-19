# Copyright 2018-2022 Streamlit Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import threading
import unittest
from typing import List, Any, Callable, cast, Optional
from unittest.mock import MagicMock, patch

import pytest
import tornado.testing
from tornado.ioloop import IOLoop

import streamlit.app_session as app_session
from streamlit import config
from streamlit.app_session import AppSession, AppSessionState
from streamlit.forward_msg_queue import ForwardMsgQueue
from streamlit.proto.ForwardMsg_pb2 import ForwardMsg
from streamlit.scriptrunner import (
    ScriptRunContext,
    add_script_run_ctx,
    get_script_run_ctx,
    ScriptRunner,
    RerunData,
)
from streamlit.scriptrunner import ScriptRunnerEvent
from streamlit.session_data import SessionData
from streamlit.state.session_state import SessionState
from streamlit.uploaded_file_manager import UploadedFileManager


@pytest.fixture
def del_path(monkeypatch):
    monkeypatch.setenv("PATH", "")


def _create_test_session(ioloop: Optional[IOLoop] = None) -> AppSession:
    """Create an AppSession instance with some default mocked data."""
    if ioloop is None:
        ioloop = MagicMock()

    return AppSession(
        ioloop=ioloop,
        session_data=SessionData("/fake/script_path", "fake_command_line"),
        uploaded_file_manager=MagicMock(),
        message_enqueued_callback=None,
        local_sources_watcher=MagicMock(),
    )


@patch("streamlit.app_session.LocalSourcesWatcher", MagicMock())
class AppSessionTest(unittest.TestCase):
    @patch("streamlit.app_session.secrets._file_change_listener.disconnect")
    def test_shutdown(self, patched_disconnect):
        """Test that AppSession.shutdown behaves sanely."""
        session = _create_test_session()

        mock_file_mgr = MagicMock(spec=UploadedFileManager)
        session._uploaded_file_mgr = mock_file_mgr

        session.shutdown()
        self.assertEqual(AppSessionState.SHUTDOWN_REQUESTED, session._state)
        mock_file_mgr.remove_session_files.assert_called_once_with(session.id)
        patched_disconnect.assert_called_once_with(session._on_secrets_file_changed)

        # A 2nd shutdown call should have no effect.
        session.shutdown()
        self.assertEqual(AppSessionState.SHUTDOWN_REQUESTED, session._state)
        mock_file_mgr.remove_session_files.assert_called_once_with(session.id)

    def test_shutdown_with_running_scriptrunner(self):
        """If we have a running ScriptRunner, shutting down should stop it."""
        session = _create_test_session()
        mock_scriptrunner = MagicMock(spec=ScriptRunner)
        session._scriptrunner = mock_scriptrunner

        session.shutdown()
        mock_scriptrunner.request_stop.assert_called_once()

        mock_scriptrunner.reset_mock()

        # A 2nd shutdown call should have no affect.
        session.shutdown()
        mock_scriptrunner.request_stop.assert_not_called()

    def test_unique_id(self):
        """Each AppSession should have a unique ID"""
        session1 = _create_test_session()
        session2 = _create_test_session()
        self.assertNotEqual(session1.id, session2.id)

    def test_creates_session_state_on_init(self):
        session = _create_test_session()
        self.assertTrue(isinstance(session.session_state, SessionState))

    def test_clear_cache_resets_session_state(self):
        session = _create_test_session()
        session._session_state["foo"] = "bar"
        session.handle_clear_cache_request()
        self.assertTrue("foo" not in session._session_state)

    @patch("streamlit.legacy_caching.clear_cache")
    @patch("streamlit.caching.memo.clear")
    @patch("streamlit.caching.singleton.clear")
    def test_clear_cache_all_caches(
        self, clear_singleton_cache, clear_memo_cache, clear_legacy_cache
    ):
        session = _create_test_session()
        session.handle_clear_cache_request()
        clear_singleton_cache.assert_called_once()
        clear_memo_cache.assert_called_once()
        clear_legacy_cache.assert_called_once()

    @patch("streamlit.app_session.secrets._file_change_listener.connect")
    def test_request_rerun_on_secrets_file_change(self, patched_connect):
        """AppSession should add a secrets listener on creation."""
        session = _create_test_session()
        patched_connect.assert_called_once_with(session._on_secrets_file_changed)

    @patch("streamlit.app_session.AppSession._create_scriptrunner")
    def test_rerun_with_no_scriptrunner(self, mock_create_scriptrunner: MagicMock):
        """If we don't have a ScriptRunner, a rerun request will result in
        one being created."""
        session = _create_test_session()
        session.request_rerun(None)
        mock_create_scriptrunner.assert_called_once_with(RerunData())

    @patch("streamlit.app_session.AppSession._create_scriptrunner")
    def test_rerun_with_active_scriptrunner(self, mock_create_scriptrunner: MagicMock):
        """If we have an active ScriptRunner, it receives rerun requests."""
        session = _create_test_session()

        mock_active_scriptrunner = MagicMock(spec=ScriptRunner)
        mock_active_scriptrunner.request_rerun = MagicMock(return_value=True)
        session._scriptrunner = mock_active_scriptrunner

        session.request_rerun(None)

        # The active ScriptRunner will accept the rerun request...
        mock_active_scriptrunner.request_rerun.assert_called_once_with(RerunData())

        # So _create_scriptrunner should not be called.
        mock_create_scriptrunner.assert_not_called()

    @patch("streamlit.app_session.AppSession._create_scriptrunner")
    def test_rerun_with_stopped_scriptrunner(self, mock_create_scriptrunner: MagicMock):
        """If have a ScriptRunner but it's shutting down and cannot handle
        new rerun requests, we'll create a new ScriptRunner."""
        session = _create_test_session()

        mock_stopped_scriptrunner = MagicMock(spec=ScriptRunner)
        mock_stopped_scriptrunner.request_rerun = MagicMock(return_value=False)
        session._scriptrunner = mock_stopped_scriptrunner

        session.request_rerun(None)

        # The stopped ScriptRunner will reject the request...
        mock_stopped_scriptrunner.request_rerun.assert_called_once_with(RerunData())

        # So we'll create a new ScriptRunner.
        mock_create_scriptrunner.assert_called_once_with(RerunData())

    @patch("streamlit.app_session.ScriptRunner")
    def test_create_scriptrunner(self, mock_scriptrunner: MagicMock):
        """Test that _create_scriptrunner does what it should."""
        session = _create_test_session()
        self.assertIsNone(session._scriptrunner)

        session._create_scriptrunner(initial_rerun_data=RerunData())

        # Assert that the ScriptRunner constructor was called.
        mock_scriptrunner.assert_called_once_with(
            session_id=session.id,
            session_data=session._session_data,
            client_state=session._client_state,
            session_state=session._session_state,
            uploaded_file_mgr=session._uploaded_file_mgr,
            initial_rerun_data=RerunData(),
        )

        self.assertIsNotNone(session._scriptrunner)

        # And that the ScriptRunner was initialized and started.
        scriptrunner: MagicMock = cast(MagicMock, session._scriptrunner)
        scriptrunner.on_event.connect.assert_called_once_with(
            session._on_scriptrunner_event
        )
        scriptrunner.start.assert_called_once()

    @patch("streamlit.app_session.ScriptRunner", MagicMock(spec=ScriptRunner))
    @patch("streamlit.app_session.AppSession._enqueue_forward_msg")
    def test_ignore_events_from_noncurrent_scriptrunner(self, mock_enqueue: MagicMock):
        """If we receive ScriptRunnerEvents from anything other than our
        current ScriptRunner, we should silently ignore them.
        """
        session = _create_test_session()
        session._create_scriptrunner(initial_rerun_data=RerunData())

        session._handle_scriptrunner_event_on_main_thread(
            sender=session._scriptrunner,
            event=ScriptRunnerEvent.ENQUEUE_FORWARD_MSG,
            forward_msg=ForwardMsg(),
        )
        mock_enqueue.assert_called_once_with(ForwardMsg())

        mock_enqueue.reset_mock()

        non_current_scriptrunner = MagicMock(spec=ScriptRunner)
        session._handle_scriptrunner_event_on_main_thread(
            sender=non_current_scriptrunner,
            event=ScriptRunnerEvent.ENQUEUE_FORWARD_MSG,
            forward_msg=ForwardMsg(),
        )
        mock_enqueue.assert_not_called()


def _mock_get_options_for_section(overrides=None) -> Callable[..., Any]:
    if not overrides:
        overrides = {}

    theme_opts = {
        "base": "dark",
        "primaryColor": "coral",
        "backgroundColor": "white",
        "secondaryBackgroundColor": "blue",
        "textColor": "black",
        "font": "serif",
    }

    for k, v in overrides.items():
        theme_opts[k] = v

    def get_options_for_section(section):
        if section == "theme":
            return theme_opts
        return config.get_options_for_section(section)

    return get_options_for_section


class AppSessionScriptEventTest(tornado.testing.AsyncTestCase):
    """Tests for AppSession's ScriptRunner event handling."""

    @patch(
        "streamlit.app_session.config.get_options_for_section",
        MagicMock(side_effect=_mock_get_options_for_section()),
    )
    @patch(
        "streamlit.app_session._generate_scriptrun_id",
        MagicMock(return_value="mock_scriptrun_id"),
    )
    @tornado.testing.gen_test
    def test_enqueue_new_session_message(self):
        """The SCRIPT_STARTED event should enqueue a 'new_session' message."""
        session = _create_test_session(self.io_loop)

        orig_ctx = get_script_run_ctx()
        ctx = ScriptRunContext(
            session_id="TestSessionID",
            enqueue=session._session_data.enqueue,
            query_string="",
            session_state=MagicMock(),
            uploaded_file_mgr=MagicMock(),
        )
        add_script_run_ctx(ctx=ctx)

        mock_scriptrunner = MagicMock(spec=ScriptRunner)
        session._scriptrunner = mock_scriptrunner

        # Send a mock SCRIPT_STARTED event.
        session._on_scriptrunner_event(
            sender=mock_scriptrunner,
            event=ScriptRunnerEvent.SCRIPT_STARTED,
        )

        # Yield to let the AppSession's callbacks run.
        yield

        sent_messages = session._session_data._browser_queue._queue
        self.assertEqual(2, len(sent_messages))  # NewApp and SessionState messages

        # Note that we're purposefully not very thoroughly testing new_session
        # fields below to avoid getting to the point where we're just
        # duplicating code in tests.
        new_session_msg = sent_messages[0].new_session
        self.assertEqual("mock_scriptrun_id", new_session_msg.script_run_id)

        self.assertTrue(new_session_msg.HasField("config"))
        self.assertEqual(
            config.get_option("server.allowRunOnSave"),
            new_session_msg.config.allow_run_on_save,
        )

        self.assertTrue(new_session_msg.HasField("custom_theme"))
        self.assertEqual("black", new_session_msg.custom_theme.text_color)

        init_msg = new_session_msg.initialize
        self.assertTrue(init_msg.HasField("user_info"))

        add_script_run_ctx(ctx=orig_ctx)

    @tornado.testing.gen_test
    def test_events_handled_on_main_thread(self):
        """ScriptRunner events should be handled on the main thread only."""
        session = _create_test_session(self.io_loop)

        # Patch the session's "_handle_scriptrunner_event_on_main_thread"
        # to test that the function is called, and that it's only called
        # on the main thread.
        def assert_is_on_main_thread(*args, **kwargs):
            self.assertEqual(threading.main_thread(), threading.current_thread())

        mock_handle_event = MagicMock(side_effect=assert_is_on_main_thread)
        session._handle_scriptrunner_event_on_main_thread = mock_handle_event

        # Send a ScriptRunner event from another thread
        thread = threading.Thread(
            target=lambda: session._on_scriptrunner_event(
                sender=MagicMock(), event=ScriptRunnerEvent.SCRIPT_STARTED
            )
        )
        thread.start()
        thread.join()

        # _handle_scriptrunner_event_on_main_thread won't have been called
        # yet, because we haven't yielded the ioloop.
        mock_handle_event.assert_not_called()

        # Yield to let the AppSession's callbacks run.
        # _handle_scriptrunner_event_on_main_thread will be called here.
        yield

        mock_handle_event.assert_called_once()

    @patch(
        "streamlit.app_session.config.get_options_for_section",
        MagicMock(side_effect=_mock_get_options_for_section()),
    )
    @patch(
        "streamlit.app_session._generate_scriptrun_id",
        MagicMock(return_value="mock_scriptrun_id"),
    )
    @tornado.testing.gen_test
    def test_handle_backmsg_exception(self):
        """handle_backmsg_exception is a bit of a hack. Test that it does
        what it says.
        """
        session = _create_test_session(self.io_loop)

        # Create a mocked ForwardMsgQueue that tracks "enqueue" and "clear"
        # function calls together in a list. We'll assert the content
        # and order of these calls.
        forward_msg_queue_events: List[Any] = []
        CLEAR_QUEUE = object()

        mock_queue = MagicMock(spec=ForwardMsgQueue)
        mock_queue.enqueue = MagicMock(
            side_effect=lambda msg: forward_msg_queue_events.append(msg)
        )
        mock_queue.clear = MagicMock(
            side_effect=lambda: forward_msg_queue_events.append(CLEAR_QUEUE)
        )

        session._session_data._browser_queue = mock_queue

        # Create an exception and have the session handle it.
        FAKE_EXCEPTION = RuntimeError("I am error")
        session.handle_backmsg_exception(FAKE_EXCEPTION)

        # Messages get sent in an ioloop callback, which hasn't had a chance
        # to run yet. Our message queue should be empty.
        self.assertEqual([], forward_msg_queue_events)

        # Run callbacks
        yield

        # Build our "expected events" list. We need to mock different
        # AppSessionState values for our AppSession to build the list.
        expected_events = []

        with patch.object(session, "_state", new=AppSessionState.APP_IS_RUNNING):
            expected_events.extend(
                [
                    session._create_script_finished_message(
                        ForwardMsg.FINISHED_SUCCESSFULLY
                    ),
                    CLEAR_QUEUE,
                    session._create_new_session_message(),
                    session._create_session_state_changed_message(),
                ]
            )

        with patch.object(session, "_state", new=AppSessionState.APP_NOT_RUNNING):
            expected_events.extend(
                [
                    session._create_script_finished_message(
                        ForwardMsg.FINISHED_SUCCESSFULLY
                    ),
                    session._create_session_state_changed_message(),
                    session._create_exception_message(FAKE_EXCEPTION),
                ]
            )

        # Assert the results!
        self.assertEqual(expected_events, forward_msg_queue_events)


class PopulateCustomThemeMsgTest(unittest.TestCase):
    @patch("streamlit.app_session.config")
    def test_no_custom_theme_prop_if_no_theme(self, patched_config):
        patched_config.get_options_for_section.side_effect = (
            _mock_get_options_for_section(
                {
                    "base": None,
                    "primaryColor": None,
                    "backgroundColor": None,
                    "secondaryBackgroundColor": None,
                    "textColor": None,
                    "font": None,
                }
            )
        )

        msg = ForwardMsg()
        new_session_msg = msg.new_session
        app_session._populate_theme_msg(new_session_msg.custom_theme)

        self.assertEqual(new_session_msg.HasField("custom_theme"), False)

    @patch("streamlit.app_session.config")
    def test_can_specify_some_options(self, patched_config):
        patched_config.get_options_for_section.side_effect = _mock_get_options_for_section(
            {
                # Leave base, primaryColor, and font defined.
                "backgroundColor": None,
                "secondaryBackgroundColor": None,
                "textColor": None,
            }
        )

        msg = ForwardMsg()
        new_session_msg = msg.new_session
        app_session._populate_theme_msg(new_session_msg.custom_theme)

        self.assertEqual(new_session_msg.HasField("custom_theme"), True)
        self.assertEqual(new_session_msg.custom_theme.primary_color, "coral")
        # In proto3, primitive fields are technically always required and are
        # set to the type's zero value when undefined.
        self.assertEqual(new_session_msg.custom_theme.background_color, "")

    @patch("streamlit.app_session.config")
    def test_can_specify_all_options(self, patched_config):
        patched_config.get_options_for_section.side_effect = (
            # Specifies all options by default.
            _mock_get_options_for_section()
        )

        msg = ForwardMsg()
        new_session_msg = msg.new_session
        app_session._populate_theme_msg(new_session_msg.custom_theme)

        self.assertEqual(new_session_msg.HasField("custom_theme"), True)
        self.assertEqual(new_session_msg.custom_theme.primary_color, "coral")
        self.assertEqual(new_session_msg.custom_theme.background_color, "white")

    @patch("streamlit.app_session.LOGGER")
    @patch("streamlit.app_session.config")
    def test_logs_warning_if_base_invalid(self, patched_config, patched_logger):
        patched_config.get_options_for_section.side_effect = (
            _mock_get_options_for_section({"base": "blah"})
        )

        msg = ForwardMsg()
        new_session_msg = msg.new_session
        app_session._populate_theme_msg(new_session_msg.custom_theme)

        patched_logger.warning.assert_called_once_with(
            '"blah" is an invalid value for theme.base.'
            " Allowed values include ['light', 'dark']. Setting theme.base to \"light\"."
        )

    @patch("streamlit.app_session.LOGGER")
    @patch("streamlit.app_session.config")
    def test_logs_warning_if_font_invalid(self, patched_config, patched_logger):
        patched_config.get_options_for_section.side_effect = (
            _mock_get_options_for_section({"font": "comic sans"})
        )

        msg = ForwardMsg()
        new_session_msg = msg.new_session
        app_session._populate_theme_msg(new_session_msg.custom_theme)

        patched_logger.warning.assert_called_once_with(
            '"comic sans" is an invalid value for theme.font.'
            " Allowed values include ['sans serif', 'serif', 'monospace']. Setting theme.font to \"sans serif\"."
        )

    def test_passes_client_state_on_run_on_save(self):
        session = _create_test_session()
        session._run_on_save = True
        session.request_rerun = MagicMock()
        session._on_source_file_changed()

        session.request_rerun.assert_called_once_with(session._client_state)