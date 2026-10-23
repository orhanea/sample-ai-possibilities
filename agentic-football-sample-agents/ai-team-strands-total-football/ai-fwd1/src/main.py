"""Maestro: false-nine-biased outfield player 3 with fluid live roles."""

from pathlib import Path
import sys

here = Path(__file__).resolve()
sys.path.insert(0, str(here.parents[1] / "lib" if (here.parents[1] / "lib").is_dir() else here.parents[3] / "lib"))

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from total_football.runtime import register

app = BedrockAgentCoreApp()
engine = register(app, 3)

if __name__ == "__main__":
    app.run()
