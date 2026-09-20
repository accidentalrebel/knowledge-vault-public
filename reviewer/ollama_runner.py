"""Isolate network reads so the parent enforces a wall-clock deadline."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reviewer.editorial import InvalidReview, Unavailable
from reviewer.providers import ollama


if __name__ == '__main__':
    try:
        request = json.load(sys.stdin)
        review = ollama(request['provider'], request['prompt'], request['timeout'], Path.cwd())
        result = {'state': 'ok', 'review': review}
    except Unavailable as exc:
        result = {'state': 'unavailable', 'reason': str(exc)}
    except (InvalidReview, ValueError, KeyError, TypeError) as exc:
        result = {'state': 'invalid', 'reason': str(exc)}
    print(json.dumps(result))
