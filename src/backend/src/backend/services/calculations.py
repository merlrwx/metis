"""Small arithmetic over values explicitly present in cited evidence."""

import re
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.services.knowledge import SearchHit


class Operand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=40)
    unit: Literal["AUD", "USD", "EUR", "GBP", "hours", "days"]
    meaning: Literal["net", "gross", "hours", "days"]
    period: str = Field(min_length=1, max_length=100)
    citation: str = Field(pattern=r"^C[1-9][0-9]*$")


class Calculation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["sum", "difference", "average"]
    operands: list[Operand] = Field(min_length=2, max_length=10)


def calculate(calculation: Calculation, evidence: list[SearchHit]) -> str:
    if calculation.operation == "difference" and len(calculation.operands) != 2:
        raise ValueError("A difference requires two operands")
    if len({(operand.unit, operand.meaning) for operand in calculation.operands}) != 1:
        raise ValueError("Operands must use the same units and meaning")
    values = []
    descriptions = []
    seen = set()
    for operand in calculation.operands:
        index = int(operand.citation[1:]) - 1
        if not 0 <= index < len(evidence):
            raise ValueError("Operand citation is outside the evidence")
        text = evidence[index].chunk.content
        try:
            value = Decimal(operand.value.replace(",", ""))
        except InvalidOperation as error:
            raise ValueError("Operand is not a decimal") from error
        if not value.is_finite() or abs(value) > Decimal("1e15"):
            raise ValueError("Operand is outside the arithmetic limits")
        present = False
        for line in text.splitlines():
            if not re.search(
                r"\b" + re.escape(operand.meaning) + r"\b", line, re.IGNORECASE
            ):
                continue
            if not re.search(
                r"\b" + re.escape(operand.unit) + r"\b", line, re.IGNORECASE
            ):
                continue
            for token in re.findall(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?![\w.])", line):
                if Decimal(token.replace(",", "")) == value:
                    present = True
        if not present or operand.period.casefold() not in text.casefold():
            raise ValueError(
                "Operand value, meaning or period is not in its cited excerpt"
            )
        identity = (
            evidence[index].document.id,
            operand.period.casefold(),
            operand.meaning,
            value,
        )
        if identity in seen:
            raise ValueError("The same operand cannot be counted twice")
        seen.add(identity)
        values.append(value)
        descriptions.append(
            f"{value} {operand.unit} {operand.meaning}, {operand.period} [{operand.citation}]"
        )
    if calculation.operation == "difference":
        result = values[0] - values[1]
        expression = f"{values[0]} − {values[1]}"
    else:
        result = sum(values, Decimal(0))
        expression = " + ".join(str(value) for value in values)
        if calculation.operation == "average":
            result /= Decimal(len(values))
            expression = f"({expression}) / {len(values)}"
    return (
        "Checked calculation: "
        + "; ".join(descriptions)
        + f". {expression} = {result} {calculation.operands[0].unit}."
    )
