// C# bridge transport skeleton using System.Text.Json (.NET 6 or later).
// Engine integration, input recording/replay and portable tape hashing are game-owned.
using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;

public static class ShakedownWire
{
    public static void Emit(TextWriter output, object message)
    {
        output.WriteLine("@@shakedown " + JsonSerializer.Serialize(message));
        output.Flush();
    }

    public static string BridgeChoice(TextReader input, TextWriter output,
        object request, long expectedSequence, ISet<string> legalNames)
    {
        if (legalNames.Count < 1 || legalNames.Count > 12)
            throw new InvalidDataException("invalid candidates");
        // The game pauses simulation here and sends hello before its first request.
        Emit(output, request);
        var text = new System.Text.StringBuilder();
        int next;
        while ((next = input.Read()) != '\n')
        {
            if (next < 0) throw new InvalidDataException("answer missing newline");
            text.Append((char)next);
        }
        using var document = JsonDocument.Parse(text.ToString());
        var answer = document.RootElement;
        if (answer.ValueKind != JsonValueKind.Object ||
            !answer.TryGetProperty("v", out var version) || !version.TryGetInt32(out var v) || v != 1 ||
            !answer.TryGetProperty("seq", out var sequence) ||
            !sequence.TryGetInt64(out var seq) || seq != expectedSequence ||
            !answer.TryGetProperty("choice", out var value) || value.ValueKind != JsonValueKind.String)
            throw new InvalidDataException("invalid answer");
        var choice = value.GetString();
        if (choice == null || !legalNames.Contains(choice))
            throw new InvalidDataException("unknown choice");
        return choice;
    }
    // Catch JSON/contract exceptions at Main and return 2; never continue silently.
    // Enqueue game input, not direct world-state changes. Oracle exits 4; tape divergence 5.
}
