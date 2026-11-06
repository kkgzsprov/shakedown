# Thin executors for discrete games

A turn-based, card, menu or puzzle game can map each legal candidate to one ordinary input event. Preserve the same observation, input, replay and contract boundaries as a real-time adapter; fewer executor states do not remove those obligations.

Construct candidates from actions the player's UI can currently perform. Keep a local `candidate name → input event` map for that decision. A selected candidate looks up one event and enqueues it before the game's normal input poll. The simulation consumes the event through the same function as a human click or button. Avoid calling outcome-changing internals such as setting health, moving a piece directly, or granting an item.

```python
legal_inputs = {"select_c2": {"button": "select", "target": "c2"},
                "pass": {"button": "pass"}}
choice, seq = session.choose(step=game.step, state=visible_state,
                            candidates=descriptions, order_seed=order_seed,
                            reason="turn_started")
# The game's own queue records and processes this event on its next input poll.
game.input_queue.append(dict(legal_inputs[choice]))
```

Record the resulting input event at the exact simulation step. Input-only replay supplies these events without reconstructing observations or choosing actions. If the game has a reaction delay or an intervening state change, revalidate the candidate on the application step and record a dropped decision when it has become illegal.

Keep hidden hands, future random draws, undiscovered tiles and opponent-private state out of observations and candidate descriptions. A positive/negative visibility fixture is still useful for a puzzle or card game: the hidden card must stay absent until an ordinary reveal action makes it visible.
