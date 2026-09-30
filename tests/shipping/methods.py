# -*- coding: utf-8 -*-
from decimal import Decimal as D

from oscar.apps.shipping.methods import FixedPrice, Free


class SecondClassRecorded(Free):
    code = 'uk_rm_2ndrecorded'
    name = 'Royal Mail Signed For™ 2nd Class'

    charge_excl_tax = D('0.00')
    charge_incl_tax = D('0.00')


class Standard(FixedPrice):
    code = 'standard'
    name = 'Standard'

    def __init__(self):
        super().__init__(charge_excl_tax=D('4.16'), charge_incl_tax=D('4.95'))
