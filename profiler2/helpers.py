# -*- coding: utf-8 -*-

"""
profiler2.helpers
~~~~~~~~~~~~~~~~~

provides init functions that are used to help setup the app.
"""

# standard library imports
import argparse, configparser, inspect, logging, logging.config, os, subprocess, sys, textwrap
from datetime import timedelta
from multiprocessing import Value
from time import time
from typing import Union

# third party imports
try:
    import manuf
    from scapy.all import (
        RadioTap,
        Dot11Elt,
        get_if_hwaddr,
        get_if_raw_hwaddr,
        Scapy_Exception,
    )
except ModuleNotFoundError as error:
    if error.name == "manuf":
        print(f"{error}. please install manuf-ng... exiting...")
    elif error.name == "scapy":
        print(f"{error}. please install scapy... exiting...")
    else:
        print(f"{error}")
    sys.exit(-1)

# is tcpdump installed?
try:
    subprocess.run(["tcpdump --version"], shell=True, check=True)
except IOError as error:
    print(f"tcpdump not found. please install tcpdump... exiting...")
    sys.exit(-1)

# app imports
from .__version__ import __author__, __version__
from .constants import CHANNELS


def setup_logger(args) -> logging.Logger:
    if args.logging:
        if args.logging == "debug":
            logging_level = logging.DEBUG
        if args.logging == "info":
            logging_level = logging.INFO
    else:
        logging_level = logging.WARNING

    default_logging = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {"format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"}
        },
        "handlers": {
            "default": {
                "level": logging_level,
                "formatter": "standard",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            }
        },
        "loggers": {"": {"handlers": ["default"], "level": logging_level}},
    }
    logging.config.dictConfig(default_logging)
    # return logging.getLogger(__name__)


def setup_parser() -> argparse:
    """Setup the parser for arguments passed into the module from the CLI.

    Returns:
        argparse object.
    """
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(
            """
            """
        ),
        epilog=f"Made with Python by {__author__}",
        fromfile_prefix_chars="2",
    )
    config = os.path.join(os.path.dirname(os.path.realpath(__file__)), "config.ini")
    parser.add_argument(
        "-i", dest="interface", help="name of network interface to bind profiler to"
    )
    parser.add_argument(
        "-c", dest="channel", help="802.11 channel for the profiler to broadcast on"
    )
    parser.add_argument("-s", dest="ssid", help="network identifier for profiler SSID")
    parser.add_argument(
        "-f",
        "-file",
        dest="file_analysis_only",
        help="read first packet of pcap file containing assoc frame",
    )
    parser.add_argument(
        "--config",
        type=str,
        metavar=".ini",
        default=config,
        help="specify path for .INI configuration file",
    )
    parser.add_argument(
        "--no11r",
        dest="ft_enabled",
        action="store_false",
        help="turn off Fast Transition (FT) reporting",
    )
    parser.add_argument(
        "--no11ax",
        dest="he_enabled",
        action="store_false",
        help="turn off High Efficiency (HE) information elements",
    )
    parser.add_argument(
        "--menu_mode",
        dest="menu_mode",
        action="store_true",
        default=False,
        help="BakeBit menu reporting",
    )
    parser.add_argument(
        "--noAP",
        dest="listen_only",
        action="store_true",
        default=False,
        help="listen only mode",
    )
    parser.add_argument(
        "--logging",
        help="increase output for debugging",
        nargs="?",
        choices=("debug", "info"),
    )
    parser.add_argument("--test", action="store_true", help="perform diagnostic tests")
    parser.add_argument(
        "--clean",
        action="store_true",
        default=False,
        help="purges client report directory",
    )
    parser.add_argument(
        "--version", "-V", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


def setup_config(args) -> dict:
    """Setup the config (SSID, channel, interface) for the Fake AP.
    
    Returns:
        dict object.
    """
    log = logging.getLogger(inspect.stack()[0][3])

    config = {}

    # load in config (a: from default location "/config.ini" or b: from provided)
    if os.path.isfile(args.config):
        config = load(args.config)

    # if couldn't find default config or user provided config
    if not config:
        config["GENERAL"] = {}

    if args.channel:
        config["GENERAL"]["channel"] = args.channel
    if args.interface:
        config["GENERAL"]["interface"] = args.interface
    if args.ssid:
        config["GENERAL"]["ssid"] = args.ssid

    # validate config.
    if validate(config):
        return config
    else:
        log.error("configuration validation failed... exiting...")
        sys.exit(-1)


def load(config_file: str) -> Union[configparser.ConfigParser, bool]:
    """ Load config file. """
    log = logging.getLogger(inspect.stack()[0][3])
    try:
        config = configparser.ConfigParser()
        config.read(config_file)
    except FileNotFoundError:
        log.exception("could not find config file")
    if config:
        return config
    else:
        return False


def validate(config: configparser.ConfigParser) -> bool:
    """ Basic config checks """
    log = logging.getLogger(inspect.stack()[0][3])
    log.info("checking config")

    if not is_root():
        return False

    if not check_config(config):
        return False

    if not is_fakeap_interface_valid(config):
        return False

    if not is_ssid_valid(config):
        return False

    if not is_channel_valid(config):
        return False

    check_reporting_dirs(config)

    log.info("finish checking config")

    return True


def is_root() -> bool:
    """ check if run with root-level privileges """
    log = logging.getLogger(inspect.stack()[0][3])
    if os.geteuid() != 0:
        log.error("must run with root privileges...")
        return False
    return True


def prep_interface(interface: str, mode: str, channel: int) -> bool:
    """ prepares the interface for monitor mode and injection """
    log = logging.getLogger(inspect.stack()[0][3])
    if mode in ("managed", "monitor"):
        commands = [
            "airmon-ng check kill",
            f"ip link set {interface} down",
            f"iw dev {interface} set type {mode}",
            f"ip link set {interface} up",
            f"iw {interface} set channel {channel}",
        ]
        try:
            [subprocess.run(c, shell=True, check=True) for c in commands]
            return True
        except Exception as error:
            log.error("error setting wlan interface config")
            log.exception(error)
    else:
        log.error("failed to prep interface config...")
        return False


def check_config(config: configparser.ConfigParser) -> bool:
    """ Check that config has expected items. """
    log = logging.getLogger(inspect.stack()[0][3])
    try:
        section = "GENERAL"
        options = [option for option in config["GENERAL"]]
        if section not in config:
            raise KeyError("missing general configuration")
        if "interface" not in options:
            raise KeyError("missing interface from config")
        if "channel" not in options:
            raise KeyError("missing channel from config")
        if "ssid" not in options:
            raise KeyError("missing ssid from config")
    except KeyError:
        log.error(sys.exc_info()[1])
        return False
    return True


def is_fakeap_interface_valid(config: configparser.ConfigParser) -> bool:
    """ Check that the config interface exists on the system. """
    log = logging.getLogger(inspect.stack()[0][3])
    discovered_interfaces = []
    interface = config.get("GENERAL", "interface")
    interface_command = (
        "find /sys/class/net -follow -maxdepth 2 -name phy80211 | cut -d / -f 5"
    )
    process = subprocess.run(
        interface_command,
        shell=True,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    discovered_interfaces = list(
        filter(None, process.stdout.decode("utf-8").split("\n"))
    )
    if interface in discovered_interfaces:
        log.info(
            f"{interface} is in discovered interfaces: {', '.join(discovered_interfaces)}"
        )
        return True
    else:
        log.critical(
            f"interface {interface} is not in discovered interfaces: {discovered_interfaces}"
        )
        return False


def is_ssid_valid(config: configparser.ConfigParser) -> bool:
    """ Checks for the configured fake AP SSID. """
    log = logging.getLogger(inspect.stack()[0][3])

    ssid = config.get("GENERAL", "ssid")
    log.info(f"ssid is {ssid}")
    if len(ssid) > 32:
        log.critical(f"ssid length cannot be greater than 32")
        return False
    return True


def is_channel_valid(config: configparser.ConfigParser) -> bool:
    """ Checks to ensure the fake AP channel is valid. """
    log = logging.getLogger(inspect.stack()[0][3])
    channel = config.get("GENERAL", "channel")
    if int(channel) in CHANNELS:
        log.info(f"{channel} is a valid 802.11 channel")
        return True
    else:
        log.critical(f"channel {channel} is not a valid channel")
        return False


def check_reporting_dirs(config: dict):
    """ Checks to ensure reporting directories exist. """
    log = logging.getLogger(inspect.stack()[0][3])

    for _ in config["REPORTING"].keys():
        if not os.path.isdir(config["REPORTING"][_]):
            log.debug(os.makedirs(config["REPORTING"][_]))


def get_frequency_bytes(channel: int) -> bytes:
    """ takes a channel number, converts it to a frequency, and finally to bytes """
    if channel == 14:
        freq = 2484
    elif channel < 14:
        freq = 2407 + (channel * 5)
    elif channel > 14:
        freq = 5000 + (channel * 5)

    return freq.to_bytes(2, byteorder="little")


def build_fake_frame_ies(ssid: str, channel: int, args) -> Dot11Elt:
    ssid = bytes(ssid, "utf-8")
    essid = Dot11Elt(ID="SSID", info=ssid)

    rates_data = [140, 18, 152, 36, 176, 72, 96, 108]
    rates = Dot11Elt(ID="Rates", info=bytes(rates_data))

    channel = bytes([channel])
    dsset = Dot11Elt(ID="DSset", info=channel)

    dtim_data = b"\x05\x04\x00\x03\x00\x00"
    dtim = Dot11Elt(ID="TIM", info=dtim_data)

    ht_cap_data = b"\xef\x19\x1b\xff\xff\xff\x00\x00\x00\x00\x00\x00\x00\x00\x00\x20\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    ht_capabilities = Dot11Elt(ID=0x2D, info=ht_cap_data)

    if args.ft_enabled:
        mobility_domain_data = b"\x45\xc2\x00"
        mobility_domain = Dot11Elt(ID=0x36, info=mobility_domain_data)
        rsn_data = b"\x01\x00\x00\x0f\xac\x04\x01\x00\x00\x0f\xac\x04\x02\x00\x00\x0f\xac\x02\x00\x0f\xac\x04\x8c\x00"
    else:
        rsn_data = b"\x01\x00\x00\x0f\xac\x04\x01\x00\x00\x0f\xac\x04\x01\x00\x00\x0f\xac\x02\x80\x00"
    rsn = Dot11Elt(ID=0x30, info=rsn_data)

    ht_info_data = (
        channel
        + b"\x00\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    )
    ht_information = Dot11Elt(ID=0x3D, info=ht_info_data)

    rm_enabled_data = b"\x02\x00\x00\x00\x00"
    rm_enabled_cap = Dot11Elt(ID=0x46, info=rm_enabled_data)

    extended_data = b"\x00\x00\x08\x00\x00\x00\x00\x40"
    extended = Dot11Elt(ID=0x7F, info=extended_data)

    vht_cap_data = b"\x32\x00\x80\x03\xaa\xff\x00\x00\xaa\xff\x00\x00"
    vht_capabilities = Dot11Elt(ID=0xBF, info=vht_cap_data)

    vht_op_data = b"\x00\x24\x00\x00\x00"
    vht_operation = Dot11Elt(ID=0xC0, info=vht_op_data)

    wmm_data = b"\x00\x50\xf2\x02\x01\x01\x8a\x00\x03\xa4\x00\x00\x27\xa4\x00\x00\x42\x43\x5e\x00\x62\x32\x2f\x00"
    wmm = Dot11Elt(ID=0xDD, info=wmm_data)

    he_cap_data = b"\x23\x09\x01\x00\x02\x40\x00\x04\x70\x0c\x80\x02\x03\x80\x04\x00\x00\x00\xaa\xff\xaa\xff\x7b\x1c\xc7\x71\x1c\xc7\x71\x1c\xc7\x71\x1c\xc7\x71"
    he_capabilities = Dot11Elt(ID=0xFF, info=he_cap_data)

    he_op_data = b"\x24\xf4\x3f\x00\x19\xfc\xff"
    he_operation = Dot11Elt(ID=0xFF, info=he_op_data)

    if args.ft_enabled:
        frame = (
            essid
            / rates
            / dsset
            / dtim
            / ht_capabilities
            / rsn
            / ht_information
            / mobility_domain
            / rm_enabled_cap
            / extended
            / vht_capabilities
            / vht_operation
        )
    else:
        frame = (
            essid
            / rates
            / dsset
            / dtim
            / ht_capabilities
            / rsn
            / ht_information
            / rm_enabled_cap
            / extended
            / vht_capabilities
            / vht_operation
        )
    if args.he_enabled:
        frame = frame / he_capabilities / he_operation / wmm
    else:
        frame = frame / wmm

    return frame


def flag_last_object(seq):
    """ treat the last object in an iterable differently """
    seq = iter(seq)  # ensure this is an iterator
    a = next(seq)
    for b in seq:
        yield a, False
        a = b
    yield a, True


def bytes_to_int(x_bytes: bytes) -> int:
    return int.from_bytes(x_bytes, "big")


def next_sequence_number(sequence_number: Value):
    """ updates a sequence number of type multiprocessing Value """
    sequence_number.value = (sequence_number.value + 1) % 4096
    return sequence_number.value


def get_radiotap_header(channel: int):
    """ builds a pseudo radio tap header """
    radiotap_packet = RadioTap(
        present="Flags+Rate+Channel+dBm_AntSignal+Antenna",
        notdecoded=b"\x8c\00"
        + get_frequency_bytes(channel)
        + b"\xc0\x00\xc0\x01\x00\x00",
    )
    return radiotap_packet


def get_mac(interface: str) -> str:
    """ gets the mac address for a specified interface """
    try:
        mac = get_if_hwaddr(interface)
    except Scapy_Exception:
        mac = ":".join(format(x, "02x") for x in get_if_raw_hwaddr(interface)[1])
    return mac


def generate_menu_report(
    menu_report_file: str,
    channel: int,
    ft_reporting: bool,
    ssid: str,
    client_count: int,
    last_manuf: str,
) -> None:
    f = open(menu_report_file, "w")
    f.writelines(
        [
            "Status: running\r",
            f"Ch:{channel} 11r:{ft_reporting}\r",
            f"SSID: {ssid}\r",
            f"Clients:{client_count} ({last_manuf})",
        ]
    )
    f.close()


def convert_timestamp_to_uptime(timestamp) -> str:
    """
    converts timestamp field from the 802.11 beacon or probe response frame to a
    human readable format. This frame is received by the WLAN interface.
    :param timestamp: unix integer representing an uptime timestamp
    :return: human readable uptime string
    """
    timestamp = timedelta(microseconds=timestamp)
    timestamp = timestamp - timedelta(microseconds=timestamp.microseconds)
    return (
        f"{str(timestamp.days).strip().zfill(2)}d "
        f"{str(timestamp).rpartition(',')[2].strip()}"
    )
