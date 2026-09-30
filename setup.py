#!/usr/bin/env python
from setuptools import find_packages, setup

from paypal import VERSION

setup(
    name='django-oscar-paypal',
    version=VERSION,
    url='https://github.com/django-oscar/django-oscar-paypal',
    description=(
        "Integration with PayPal Checkout (Orders v2), PayPal Express and "
        "PayPal Payflow Pro for django-oscar"),
    long_description=open('README.rst').read(),
    keywords="Payment, PayPal, Oscar",
    license='BSD',
    platforms=['linux'],
    packages=find_packages(exclude=['sandbox*', 'tests*']),
    include_package_data=True,
    install_requires=[
        'django>=4.2,<6.2',
        'requests>=2.26.0',
        'django-localflavor'
    ],
    extras_require={
        'oscar': ['django-oscar>=3.2,<5.0']
    },
    # See http://pypi.python.org/pypi?%3Aaction=list_classifiers
    classifiers=[
        'Development Status :: 4 - Beta',
        'Environment :: Web Environment',
        'Framework :: Django',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: BSD License',
        'Operating System :: Unix',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Programming Language :: Python :: 3.13',
        'Framework :: Django :: 4.2',
        'Framework :: Django :: 5.2',
        'Framework :: Django :: 6.0',
        'Topic :: Other/Nonlisted Topic'],
)
