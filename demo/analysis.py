def analyze(df):
    rows = []
    grouped = df.groupby("COL_0001", sort=True)
    for group, sub in grouped:
        total = sub["COL_0002"].sum()
        exc = sub["COL_0003"].sum()
        result = None if total == 0 else (total - exc) / total
        rows.append({"group": group, "result": result})
    rows = rows[:1000]
    return {"rows": rows}
