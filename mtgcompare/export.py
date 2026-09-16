"""Turn a buy plan into something you can keep.

Two formats: a CSV for a spreadsheet or a record of what you paid, and a plain
text summary that reads like a shopping list.
"""

import csv
import datetime
import io


def _today():
    return datetime.date.today().isoformat()


def to_csv(plan, cards=None):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Shop", "Qty", "Card", "Condition", "Finish",
                     "Unit price", "Line total", "Listing", "Exported"])
    today = _today()
    for shop in (plan or {}).get("shops", []):
        for line in shop["lines"]:
            writer.writerow([
                shop["name"], line["qty"], line["name"],
                line["condition"] if line["condition_known"] else line["condition"] + " (not stated)",
                "Foil" if line["foil"] else "Non-foil",
                "%.2f" % line["unit_price"], "%.2f" % line["line_total"],
                line["product_url"], today,
            ])

    writer.writerow([])
    writer.writerow(["", "", "Total", "", "", "", "%.2f" % (plan or {}).get("total", 0)])

    for name in (plan or {}).get("unavailable", []):
        writer.writerow(["", "", name, "NOT FOUND IN STOCK", "", "", "", "", today])
    for item in (plan or {}).get("filtered_out", []):
        writer.writerow([item["shop"], "", item["name"],
                         "IN STOCK BUT %s" % item["condition"].upper(),
                         "Foil" if item["foil"] else "Non-foil",
                         "%.2f" % item["cheapest"], "", item.get("url", ""), today])
    for item in (plan or {}).get("excluded", []):
        writer.writerow([" / ".join(item["shops"]), "", item["name"],
                         "IN STOCK BUT OVER YOUR SHOP LIMIT", "",
                         "%.2f" % item["cheapest"], "", "", today])
    return buffer.getvalue()


def to_text(plan, cart_links=None):
    plan = plan or {}
    cart_links = cart_links or {}
    out = ["MTG Compare buy plan - %s" % _today(), ""]
    for shop in plan.get("shops", []):
        out.append("%s  -  %s" % (shop["name"], _money(shop["subtotal"])))
        for line in shop["lines"]:
            finish = " foil" if line["foil"] else ""
            out.append("    %d x %s (%s%s)  %s" % (
                line["qty"], line["name"], line["condition"], finish, _money(line["line_total"])))
        link = cart_links.get(shop["id"])
        if link:
            out.append("    basket: %s" % link)
        out.append("")
    out.append("TOTAL %s  (cards only - each shop adds its own delivery at checkout)"
               % _money(plan.get("total", 0)))
    if plan.get("unavailable"):
        out += ["", "Not in stock at any shop checked:"]
        out += ["    %s" % name for name in plan["unavailable"]]
    if plan.get("filtered_out"):
        out += ["", "In stock, but not in the condition or finish you asked for:"]
        out += ["    %s - %s at %s, but %s%s" % (i["name"], _money(i["cheapest"]), i["shop"],
                                                 i["condition"], " foil" if i["foil"] else "")
                for i in plan["filtered_out"]]
    if plan.get("excluded"):
        out += ["", "In stock, but left out to keep within your shop limit:"]
        out += ["    %s - from %s at %s" % (item["name"], _money(item["cheapest"]),
                                            " or ".join(item["shops"]))
                for item in plan["excluded"]]
    out += ["", "Prices were correct when exported and change constantly - check the basket before paying."]
    return "\n".join(out)


def _money(value):
    return "£%.2f" % float(value or 0)
