"""Smoke-tier fixtures: build the image once per session.

Smoke tier = "does the container boot at all". Tests here shell out to
``docker`` and rely only on the image — no postgres, no slack_mock, no
compose stack. Anything needing a live HTTP surface goes to tier 3.
"""
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE_TAG = 'veloslab/alerta-test:smoke'


@pytest.fixture(scope='session')
def alerta_image():
    """Build the alerta image once, reuse across all smoke tests.

    Returns:
        The image tag as a string. Subsequent ``docker run`` calls can
        pass this to execute commands inside the built image.

    Raises:
        pytest.skip: If the docker CLI isn't on PATH. Keeps CI jobs that
            don't have docker (pure unit runners) green.
    """
    if subprocess.run(['which', 'docker'], capture_output=True).returncode != 0:
        pytest.skip('docker CLI not available')

    result = subprocess.run(
        ['docker', 'build', '-t', IMAGE_TAG, str(REPO_ROOT)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Surface the build log in the failure message — the single
        # most common reason a smoke test fails is the image didn't
        # build, and the default pytest traceback buries stderr.
        pytest.fail(f'docker build failed:\n{result.stderr[-4000:]}')
    return IMAGE_TAG


def run_in_image(image: str, *cmd: str) -> subprocess.CompletedProcess:
    """Run a command inside a fresh throwaway container.

    Args:
        image: Tag produced by the ``alerta_image`` fixture.
        *cmd: Command and arguments to execute. Joined as the container's
            entrypoint override.

    Returns:
        The ``CompletedProcess`` result. Callers assert on ``returncode``
        and ``stdout``/``stderr`` as needed.
    """
    return subprocess.run(
        ['docker', 'run', '--rm', '--entrypoint', cmd[0], image, *cmd[1:]],
        capture_output=True,
        text=True,
        timeout=60,
    )
