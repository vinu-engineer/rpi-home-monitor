"""Tests for camera_streamer main entry point."""
from camera_streamer.main import main


class TestMain:
    """Test the main entry point."""

    def test_main_callable(self):
        assert callable(main)

    def test_main_runs_without_error(self):
        """Main is currently a stub — it should run and return cleanly."""
        main()
