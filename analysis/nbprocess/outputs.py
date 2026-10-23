from .diff import get_ops

def is_float(value):
    try:
        float(value)
        return True
    except Exception:
        return False

def extract_output_values(output_diff):
    ops = get_ops(output_diff)

    for op in ops:
        if op.get("op") == "replace":
            return str(op.get("value")), str(op.get("value")), "unknown"

        if op.get("op") == "patch":
            for subop in get_ops(op.get("diff")):
                key = subop.get("key")

                if key == "text":
                    return str(subop.get("value")), str(subop.get("value")), "stream"

                if key == "data":
                    for mimeop in get_ops(subop.get("diff")):
                        mime = mimeop.get("key")
                        if isinstance(mimeop.get("value"), (str, int, float)):
                            return (
                                str(mimeop.get("value")),
                                str(mimeop.get("value")),
                                mime,
                            )

    return None, None, None
