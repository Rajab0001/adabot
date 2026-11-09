import json
import sqlite3
from contextlib import nullcontext
from pathlib import Path


MENU = json.loads(Path(__file__).with_name("menu.json").read_text(encoding="utf-8"))


def money(amount):
    return f"₦{amount:,}"


class OrderChat:
    def __init__(self, database, customer=None):
        self.database = database
        self.customer = customer
        database.execute(
            "CREATE TABLE IF NOT EXISTS orders "
            "(id INTEGER PRIMARY KEY, details TEXT NOT NULL)"
        )
        self.reset()

    def reset(self):
        self.stage = "menu"
        self.items = []
        self.cart = {}
        self.fulfillment = ""
        self.address = ""

    def summary(self):
        lines = [
            f"{quantity} × {name} — {money(quantity * price)}"
            for (name, price), quantity in self.cart.items()
        ]
        total = sum(price * qty for (_, price), qty in self.cart.items())
        return "\n".join([*lines, f"Food total: {money(total)}"])

    def reply(self, message):
        message = message.strip()
        command = message.casefold()
        if command == "cancel":
            self.reset()
            return "Cart cleared. Type menu to start again."
        if command == "help":
            return "In the live bot, staff will handle help requests. Type menu to browse or cancel to clear your cart."
        if command in {"hi", "menu", "back"}:
            self.stage = "menu"
            categories = "\n".join(
                f"{i}. {name}" for i, name in enumerate(MENU, 1)
            )
            return f"Welcome to Ada Place! Pick a category.\n{categories}\nType cart to review or help for assistance."
        if command == "cart":
            self.stage = "cart"
            return self.summary() + "\nType checkout or menu." if self.cart else "Your cart is empty. Type menu."
        if command == "checkout":
            if not self.cart:
                return "Add an item first. Type menu."
            self.stage = "fulfillment"
            self.address = ""
            return "Pickup or delivery?"
        if self.stage == "fulfillment" and command in {"pickup", "delivery"}:
            self.fulfillment = command
            if command == "delivery":
                self.stage = "address"
                return "Enter your delivery address. Staff will confirm the delivery fee."
            return self.review()
        if self.stage == "address" and message:
            self.address = message
            return self.review()
        if self.stage == "review" and command == "place order":
            order = dict(
                items=[dict(name=name, price=price, quantity=qty)
                       for (name, price), qty in self.cart.items()],
                fulfillment=self.fulfillment,
                address=self.address,
            )
            if self.customer:
                order["customer"] = self.customer
            with self.database if self.customer is None else nullcontext():
                row = self.database.execute(
                    "INSERT INTO orders (details) VALUES (?)", (json.dumps(order),)
                )
            self.reset()
            if self.customer:
                return f"Order AP-{row.lastrowid:04d} received. Please wait for staff confirmation."
            return f"Test order AP-{row.lastrowid:04d} received. Saved locally for review."
        number = int(command) if command.isascii() and command.isdecimal() and len(command) <= 3 else 0
        if self.stage == "menu" and 1 <= number <= len(MENU):
            category = list(MENU)[number - 1]
            self.items = MENU[category]
            self.stage = "item"
            return "\n".join([category, *(
                f"{i}. {name} — {money(price)}"
                for i, (name, price) in enumerate(self.items, 1)
            ), "Choose an item or type back."])
        if self.stage == "item" and 1 <= number <= len(self.items):
            self.selected = tuple(self.items[number - 1])
            self.stage = "quantity"
            return f"How many orders of {self.selected[0]}? Enter 1–99."
        if self.stage == "quantity" and 1 <= number <= 99:
            self.cart[self.selected] = self.cart.get(self.selected, 0) + number
            self.stage = "cart"
            return "Added to your cart. Type menu, cart, or checkout."
        return "Please use one of the options above, or type menu to browse."

    def review(self):
        self.stage = "review"
        details = "Pickup" if self.fulfillment == "pickup" else (
            f"Delivery to {self.address}\nDelivery fee pending staff confirmation"
        )
        return f"{self.summary()}\n{details}\nType place order to submit or menu to keep shopping."


if __name__ == "__main__":
    database = sqlite3.connect(Path(__file__).with_name("test-orders.sqlite3"))
    chat = OrderChat(database)
    print(chat.reply("hi"))
    try:
        while True:
            message = input("\nYou: ")
            if message.strip().casefold() == "quit":
                break
            print(chat.reply(message))
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        database.close()
