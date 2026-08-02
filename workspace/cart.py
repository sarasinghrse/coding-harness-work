"""A small shopping cart module.

Users have reported three bugs:
1. Applying a discount twice compounds it instead of keeping it at the
   most recent value.
2. Removing an item that is not in the cart crashes instead of being a
   no-op.
3. The total ignores item quantities.
"""


class ShoppingCart:
    def __init__(self):
        self.items = {}  # name -> {"price": float, "quantity": int}
        self.discount_percent = 0.0

    def add_item(self, name, price, quantity=1):
        if name in self.items:
            self.items[name]["quantity"] += quantity
        else:
            self.items[name] = {"price": price, "quantity": quantity}

    def remove_item(self, name):
        # Safely remove an item; ignore if it doesn't exist
        self.items.pop(name, None)

    def apply_discount(self, percent):
        # Replace any existing discount with the new value
        self.discount_percent = percent

    def total(self):
        # Calculate subtotal accounting for quantities
        subtotal = sum(item["price"] * item["quantity"] for item in self.items.values())
        return round(subtotal * (1 - self.discount_percent / 100), 2)
