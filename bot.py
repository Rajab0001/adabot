import json
import os
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
        self.order_id = None
        self.selected_image = None

    def payment_instructions(self, order_id, total, fulfillment, pending_price):
        bank = os.environ.get("PAYMENT_BANK", "").strip()
        account_number = os.environ.get("PAYMENT_ACCOUNT_NUMBER", "").strip()
        account_name = os.environ.get("PAYMENT_ACCOUNT_NAME", "").strip()
        if not all((bank, account_number, account_name)):
            return (
                f"Order AP-{order_id:04d} received. Payment instructions are not available yet. "
                "Please contact staff before making a transfer."
            )
        if pending_price:
            amount_line = (
                f"Current subtotal: {money(total)}. One or more item prices are pending staff confirmation. "
                "Wait for staff to confirm the final amount before transferring."
            )
        elif fulfillment == "pickup":
            amount_line = f"Amount to transfer: {money(total)}"
        else:
            amount_line = (
                f"Food subtotal: {money(total)}\n"
                "Delivery fee is pending staff confirmation. Wait for staff to confirm the final amount before transferring."
            )
        return (
            f"Order AP-{order_id:04d} received.\n"
            f"{amount_line}\n"
            f"Bank: {bank}\nAccount number: {account_number}\n"
            f"Account name: {account_name}\n\n"
            "After transferring the confirmed amount, reply PAID. Your order will remain pending until staff verifies the payment."
        )

    def summary(self):
        lines = []
        total = 0
        pending_price = False
        for (name, price), quantity in self.cart.items():
            if price is None:
                lines.append(f"{quantity} × {name} — price to be confirmed")
                pending_price = True
            else:
                lines.append(f"{quantity} × {name} — {money(quantity * price)}")
                total += quantity * price
        total_text = f"Subtotal: {money(total)}"
        if pending_price:
            total_text += " (plus item(s) awaiting price confirmation)"
        return "\n".join([*lines, total_text])

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
            total = sum((price or 0) * qty for (_, price), qty in self.cart.items())
            pending_price = any(price is None for (_, price) in self.cart)
            order = dict(
                items=[dict(name=name, price=price, quantity=qty)
                       for (name, price), qty in self.cart.items()],
                fulfillment=self.fulfillment,
                address=self.address,
                total=total,
                price_pending=pending_price,
                payment_status="pending",
            )
            if self.customer:
                order["customer"] = self.customer
            with self.database if self.customer is None else nullcontext():
                row = self.database.execute(
                    "INSERT INTO orders (details) VALUES (?)", (json.dumps(order),)
                )
            if self.customer:
                self.order_id = row.lastrowid
                self.stage = "payment"
                return self.payment_instructions(row.lastrowid, total, self.fulfillment, pending_price)
            self.reset()
            return f"Test order AP-{row.lastrowid:04d} received. Saved locally for review."

        if self.stage == "payment" and command == "paid" and self.order_id:
            row = self.database.execute(
                "SELECT details FROM orders WHERE id = ?", (self.order_id,)
            ).fetchone()
            if row:
                order = json.loads(row[0])
                order["payment_status"] = "reported_by_customer"
                self.database.execute(
                    "UPDATE orders SET details = ? WHERE id = ?",
                    (json.dumps(order), self.order_id),
                )
                self.stage = "payment_reported"
                return (
                    f"Thanks. Order AP-{self.order_id:04d} is marked as payment reported, "
                    "not yet verified. Staff will confirm once the transfer is checked."
                )
            return "I couldn't find that order. Please contact staff for help."
        number = int(command) if command.isascii() and command.isdecimal() and len(command) <= 3 else 0
        if self.stage == "menu" and 1 <= number <= len(MENU):
            category = list(MENU)[number - 1]
            self.items = MENU[category]
            self.stage = "item"
            return "\n".join([category, *(
                f"{i}. {name} — {money(price) if price is not None else 'price confirmed by staff'}"
                for i, (name, price, *_) in enumerate(self.items, 1)
            ), "Choose an item or type back."])
        if self.stage == "item" and 1 <= number <= len(self.items):
            item = self.items[number - 1]
            self.selected = (item[0], item[1])
            self.selected_image = item[2] if len(item) > 2 else None
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
