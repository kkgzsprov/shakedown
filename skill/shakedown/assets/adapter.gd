# Godot 4 bridge transport skeleton. The host supplies blocking UTF-8 line I/O.
# write_and_flush receives a complete line and must flush stdout immediately.
# read_complete_line returns a newline-terminated line, or "" at EOF.
# Keep simulation paused until this exchange finishes. These hooks are not an executor.
class_name ShakedownWire
extends RefCounted

var write_and_flush: Callable
var read_complete_line: Callable

func _init(writer: Callable, reader: Callable) -> void:
	write_and_flush = writer
	read_complete_line = reader

func emit(message: Dictionary) -> void:
	write_and_flush.call("@@shakedown " + JSON.stringify(message) + "\n")

func bridge_choice(request: Dictionary) -> Dictionary:
	# The game sends exactly one hello before the first request.
	var candidates = request.get("candidates", {})
	if not candidates is Dictionary or candidates.size() < 1 or candidates.size() > 12:
		return {"exit_code": 2}
	emit(request)
	var line = read_complete_line.call()
	if not line is String or not line.ends_with("\n"):
		return {"exit_code": 2}
	var answer = JSON.parse_string(line)
	if not answer is Dictionary:
		return {"exit_code": 2}
	var sequence = answer.get("seq")
	# Godot JSON numbers may be floats: permit only exactly integral safe integers.
	if not (sequence is int or sequence is float):
		return {"exit_code": 2}
	if not is_finite(float(sequence)) or abs(sequence) > 9007199254740991 or sequence != floor(sequence):
		return {"exit_code": 2}
	var version = answer.get("v")
	if not (version is int or version is float) or version != 1 or sequence != request["seq"]:
		return {"exit_code": 2}
	var choice = answer.get("choice")
	if not choice is String or not candidates.has(choice):
		return {"exit_code": 2}
	return {"exit_code": 0, "choice": choice}

# On exit_code 2 terminate the process without another decision. Oracle exit is 4.
# Input replay and canonical UTF-8 request hashing for tape mode require game integration.
