"""Link notifier: announces new tickets and outcomes, and never accepts commands."""
import hashlib

from . import jev_channel
from .approvals import dashboard_url
from .worker import BLOCK_KEY, worker_lock


def announce_block(desk):
    """One message per distinct cause, not one per minute.

    A paused reviewer is the failure that looks exactly like nothing happening, so it has
    to say so itself; the dashboard carries it for as long as it lasts.
    """
    blocked = desk.store.get_meta(BLOCK_KEY)
    if not blocked:
        return None
    detail = blocked.get('detail', 'unknown')
    desk._notify('review-blocked:' + hashlib.sha256(detail.encode()).hexdigest(),
                 'Knowledge vault: review is paused.\n%s\n'
                 'Nothing is lost and nothing will publish until it clears.' % detail)
    return detail


def tick(desk, config):
    with worker_lock(desk.store) as acquired:
        if not acquired:
            return {'state': 'busy'}
        desk.recover()
        result = desk.digest(dashboard_url(config))
        # Decide inside the same run so a ticket never sits announced-but-undecided.
        gate = jev_channel.tick(desk, config)
        blocked = announce_block(desk)
        desk.notify_revision_results()
        desk.flush_responses()
        summary = {'state': 'checked', 'digest': result}
        if blocked:
            summary['review_blocked'] = blocked
        # Absent when the gate is off, so a disabled feature changes no existing output.
        if gate.get('state') != 'disabled':
            summary['jev'] = gate
        return summary
