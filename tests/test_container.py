"""Tests for container startup behavior."""
import contextlib
import io
import os
import tempfile
import unittest
from unittest.mock import patch

from bgpcanary import auth, container, db


class InitialAdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "bgpcanary.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _start(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            container.initial_admin(self.db_path)
        return output.getvalue()

    def test_default_logs_password_and_keeps_private_file_until_rotation(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BGPCANARY_LOG_INITIAL_PASSWORD", None)
            output = self._start()
        self.assertTrue("password: " in output, "default startup must retain the historical password notice")
        pw_file = os.path.join(self.tmp.name, "initial-admin-password")
        self.assertEqual(os.stat(pw_file).st_mode & 0o777, 0o600)
        with open(pw_file, encoding="utf-8") as f:
            initial_pw = f.read().split("password: ", 1)[1].splitlines()[0]
        self.assertTrue(initial_pw in output, "default startup must print the generated password")

        conn = db.connect(self.db_path)
        try:
            self.assertTrue(auth.verify_password(initial_pw, conn.execute(
                "SELECT pw_hash FROM users WHERE username='admin'").fetchone()[0]),
                "the logged password must authenticate")
        finally:
            conn.close()

        self.assertFalse(self._start(), "repeat startup must not print another initial password")
        conn = db.connect(self.db_path)
        try:
            auth.set_user(conn, "admin", "Synthetic-Rotated-Password-1", must_change=0)
        finally:
            conn.close()
        self._start()
        self.assertFalse(os.path.exists(pw_file), "rotation must remove the temporary password file")

    def test_opt_out_keeps_password_private_file_and_account(self):
        with patch.dict(os.environ, {"BGPCANARY_LOG_INITIAL_PASSWORD": "0"}):
            self.assertFalse(self._start(), "opt-out startup must not print the initial password")
            pw_file = os.path.join(self.tmp.name, "initial-admin-password")
            self.assertEqual(os.stat(pw_file).st_mode & 0o777, 0o600)
            with open(pw_file, encoding="utf-8") as f:
                initial_pw = f.read().split("password: ", 1)[1].splitlines()[0]
            conn = db.connect(self.db_path)
            try:
                row = conn.execute("SELECT pw_hash, must_change FROM users WHERE username='admin'").fetchone()
                self.assertIsNotNone(row, "opt-out must still create the admin account")
                self.assertEqual(row["must_change"], 1, "the first-login password rotation requirement must remain")
                self.assertTrue(auth.verify_password(initial_pw, row["pw_hash"]),
                                "the private file password must authenticate")
            finally:
                conn.close()

            self.assertFalse(self._start(), "repeat startup must stay quiet")
            self.assertTrue(os.path.isfile(pw_file), "repeat startup must keep the password file until rotation")
            conn = db.connect(self.db_path)
            try:
                auth.set_user(conn, "admin", "Synthetic-Rotated-Password-1", must_change=0)
            finally:
                conn.close()
            self._start()
            self.assertFalse(os.path.exists(pw_file), "rotation must remove the temporary password file")


if __name__ == "__main__":
    unittest.main()
