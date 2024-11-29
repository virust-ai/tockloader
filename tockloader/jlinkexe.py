"""
Interface for boards using Segger's JLinkExe program.

All communication with the board is done using JLinkExe commands and scripts.

Different MCUs require different command line arguments so that the JLinkExe
tool knows which JTAG interface it is talking to. Since we don't want to burden
the user with specifying the board each time, we default to using a generic
cortex-m0 target, and use that to read the bootloader attributes to get the
correct version. Once we know more about the board we are talking to we use the
correct command line argument for future communication.
"""

import logging
import os
import platform
import subprocess
import tempfile
import time

from .board_interface import BoardInterface
from .exceptions import TockLoaderException, ChannelAddressErrorException


class JLinkExe(BoardInterface):
    def __init__(self, args):
        # Must call the generic init first.
        super().__init__(args)

        # Determine the name of the binary to run.
        self.jlink_cmd = getattr(self.args, "jlink_cmd")
        # If not specified we use the default, but this is different on Windows
        # vs. not Windows.
        if self.jlink_cmd == None:
            self.jlink_cmd = "JLinkExe"
            if platform.system() == "Windows":
                self.jlink_cmd = "JLink"

        # By default we assume that jlinkexe can be used to read any address on
        # this board, so we set `address_maximum` to None. In some cases,
        # however, particularly with external flash chips, jlinkexe may not be
        # able to read all flash addresses tockloader needs to access.
        self.address_maximum = None

    def attached_board_exists(self):
        # Get a list of attached jlink devices, check if that list has at least
        # one entry.
        emulators = self._list_emulators()
        print("List of Debugger Available {}".format(emulators))
        return self._get_tockloader_board_from_emulators(emulators) != None

    def _get_tockloader_board_from_emulators(self, emulators):
        """
        Returns None or a board name if we can parse the emulators list
        and find a valid board.

        To add to this list, connect your board, then:

        $ JLinkExe
        > ShowEmuList

        and hope there is something unique we can match on.
        """
        if len(emulators) > 0:
            # Just use the first one. Should be good enough to just assume
            # there is only one for now.
            emulator = emulators[0]
            # Check for known JTAG board.
            if emulator["ProductName"] == "J-Link PRO":
                # This seems to match both the nRF52dk (PCA10040) and the
                # nRF52840dk (PCA10056). From a jlink perspective, they are
                # close enough, which is nice.
                return "stm32f4discovery"
            if emulator["ProductName"] == "J-Link (unknown)":
                logging.warning("JLink EmuList reports 'unknown' product.")
                logging.warning("Please try upgrading JLinkExe to latest version.")
            if emulator["ProductName"] == "J-Link OB-SAM3U128-V2-NordicSem":
                # This seems to match both the nRF52dk (PCA10040) and the
                # nRF52840dk (PCA10056). From a jlink perspective, they are
                # close enough, which is nice.
                return "nrf52dk"
            if emulator["ProductName"] == "J-Link OB-nRF5340-NordicSemi":
                # This matches the new (3.0.0+) version of the nRF52840dk
                # (PCA10056).
                return "nrf52dk"
            if emulator["ProductName"] == "J-Link" and emulator[
                "Serial number"
            ].startswith("97900"):
                # SiFive did us no favors with how they set the Product Name.
                # But two boards had the same numbers to start the serial
                # number, so we try to leverage those as well. Who knows how
                # well this will work.
                return "hifive1b"
            if emulator["ProductName"] == "J-Link OB-K22-SiFive":
                # It appears perhaps a newer jlink actually gets a full product
                # name from the chip?? Well, let's try not to break backwards
                # compatibility and just add another entry.
                return "hifive1b"
            if emulator["ProductName"] == "J-Link OB-STM32F072-128KB-Corte":
                # In Aug 2021 I found this for the aconno ACD52832. We don't
                # have an ACD52832-specific board, so we just use the nrf52dk.
                return "nrf52dk"

        return None

    def open_link_to_board(self):
        # Use command line arguments to set the necessary options.
        self.jlink_device = getattr(self.args, "jlink_device")
        self.jlink_speed = getattr(self.args, "jlink_speed")
        self.jlink_if = getattr(self.args, "jlink_if")

        # It's very important that we know the jlink-device. There are three
        # ways we can learn that: 1) use the known boards struct, 2) have it
        # passed in via a command line option, 3) guess it from the jlink
        # device. If options 1 and 2 aren't done, then we try number 3!
        if self.board == None and self.jlink_device == "cortex-m0":
            emulators = self._list_emulators()
            board = self._get_tockloader_board_from_emulators(emulators)
            if board:
                self.board = board

        # If the user specified a board, use that configuration to fill in any
        # missing settings.
        if self.board and self.board in self.KNOWN_BOARDS:
            logging.info('Using settings from KNOWN_BOARDS["{}"]'.format(self.board))
            board = self.KNOWN_BOARDS[self.board]

            # Set required settings
            if self.jlink_device == "cortex-m0":
                if "jlink" in board:
                    self.jlink_device = board["jlink"]["device"]

            # Set optional settings
            if self.jlink_if == None and "if" in board["jlink"]:
                self.jlink_if = board["jlink"]["if"]
            if self.jlink_speed == None and "speed" in board["jlink"]:
                self.jlink_speed = board["jlink"]["speed"]
            if self.address_maximum == None and "address_maximum" in board["jlink"]:
                self.address_maximum = board["jlink"]["address_maximum"]

            # And we may need to setup other common board settings.
            self._configure_from_known_boards()

        if self.jlink_device == "cortex-m0":
            raise TockLoaderException(
                "Unknown JLink Device type. You must pass --jlink-device."
            )

        # If certain settings are still missing, use defaults.
        if self.jlink_if == None:
            self.jlink_if = "swd"
        if self.jlink_speed == None:
            self.jlink_speed = 1200

    def _run_jtag_commands(self, commands, binary, write=True):
        """
        - `commands`: List of JLinkExe commands. Use {binary} for where the name
          of the binary file should be substituted.
        - `binary`: A bytes() object that will be used to write to the board.
        - `write`: Set to true if the command writes binaries to the board. Set
          to false if the command will read bits from the board.
        """

        # On Windows, do not delete temp files because they delete too fast.
        delete = platform.system() != "Windows"
        if self.args.debug:
            delete = False

        if binary or not write:
            temp_bin = tempfile.NamedTemporaryFile(
                mode="w+b", suffix=".bin", delete=delete
            )
            if write:
                temp_bin.write(binary)

            temp_bin.flush()

            # On Windows we have set the files to not delete, so closing them
            # will not cause them to be removed. However, we close them to avoid
            # file locking issues.
            if platform.system() == "Windows":
                temp_bin.close()

            # Update all of the commands with the name of the binary file
            for i, command in enumerate(commands):
                commands[i] = command.format(binary=temp_bin.name)

        with tempfile.NamedTemporaryFile(mode="w", delete=delete) as jlink_file:
            for command in commands:
                jlink_file.write(command + "\n")

            jlink_file.flush()

            if platform.system() == "Windows":
                jlink_file.close()

            jlink_command = "{} -device {} -if {} -speed {} -AutoConnect 1 -jtagconf -1,-1 -CommanderScript {}".format(
                self.jlink_cmd,
                self.jlink_device,
                self.jlink_if,
                self.jlink_speed,
                jlink_file.name,
            )

            logging.debug('Running "{}".'.format(jlink_command))

            def print_output(subp):
                if subp.stdout:
                    logging.info(subp.stdout.decode("utf-8"))
                if subp.stderr:
                    logging.info(subp.stderr.decode("utf-8"))

            p = subprocess.run(
                jlink_command.split(), stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            if p.returncode != 0:
                logging.error(
                    "ERROR: JTAG returned with error code " + str(p.returncode)
                )
                print_output(p)
                raise TockLoaderException("JTAG error")
            elif self.args.debug:
                print_output(p)

            # check that there was a JTAG programmer and that it found a device
            stdout = p.stdout.decode("utf-8")
            if "USB...FAILED" in stdout:
                raise TockLoaderException(
                    "ERROR: Cannot find JLink hardware. Is USB attached?"
                )
            if (
                "Can not connect to target." in stdout
                or "Cannot connect to target." in stdout
            ):
                raise TockLoaderException(
                    "ERROR: Cannot find device. Is JTAG connected?"
                )
            if "Error while programming flash" in stdout:
                raise TockLoaderException("ERROR: Problem flashing.")

        # On Windows we need to re-open files to do a possible read, and cleanup
        # files that we could not set to auto delete.
        if platform.system() == "Windows":
            ret = None
            if write == False:
                # Wanted to read binary, so lets pull that
                with open(temp_bin.name, "rb") as temp_bin:
                    temp_bin.seek(0, 0)
                    ret = temp_bin.read()

            # Cleanup files on Windows if needed.
            if not self.args.debug:
                os.remove(jlink_file.name)
                os.remove(temp_bin.name)

            return ret

        if write == False:
            # Wanted to read binary, so lets pull that
            temp_bin.seek(0, 0)
            return temp_bin.read()

    def _list_emulators(self):
        """
        Retrieve a list of JLink compatible devices.
        """
        # On Windows, do not delete temp files because they delete too fast.
        delete = platform.system() != "Windows"
        if self.args.debug:
            delete = False

        emulators = []

        with tempfile.NamedTemporaryFile(mode="w", delete=delete) as jlink_file:
            jlink_file.write("ShowEmuList\nq")
            jlink_file.flush()

            if platform.system() == "Windows":
                jlink_file.close()

            jlink_command = "{} -CommanderScript {}".format(
                self.jlink_cmd, jlink_file.name
            )

            logging.debug('Running "{}".'.format(jlink_command))

            def print_output(subp):
                if subp.stdout:
                    logging.info(subp.stdout.decode("utf-8"))
                if subp.stderr:
                    logging.info(subp.stderr.decode("utf-8"))

            try:
                p = subprocess.run(
                    jlink_command.split(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                if p.returncode != 0:
                    logging.error(
                        "ERROR: JTAG returned with error code " + str(p.returncode)
                    )
                    print_output(p)
                    raise TockLoaderException("JTAG error")
                elif self.args.debug:
                    print_output(p)

                # check that there was a JTAG programmer and that it found a device
                stdout = p.stdout.decode("utf-8")

                # Parse out the connected emulators
                for l in stdout.split("\n"):
                    if "J-Link[" in l:
                        emulator = {}
                        parameters_string = l.split(":", 1)[1]
                        parameters = parameters_string.split(",")
                        for parameter in parameters:
                            kvs = parameter.split(":")
                            emulator[kvs[0].strip()] = kvs[1].strip()
                        emulators.append(emulator)
            except FileNotFoundError as e:
                if self.args.debug:
                    logging.debug("JLink tool does not seem to exist.")
                    logging.debug(e)
            except:
                # Any other error just ignore...this is only for convenience.
                pass

        # On Windows we need to re-open files to do a possible read, and cleanup
        # files that we could not set to auto delete.
        if platform.system() == "Windows":
            # Cleanup files on Windows if needed.
            if not self.args.debug:
                os.remove(jlink_file.name)
                os.remove(temp_bin.name)

        return emulators

    def flash_binary(self, address, binary, pad=False):
        """
        Write using JTAG
        """
        if self.address_maximum and address > self.address_maximum:
            raise ChannelAddressErrorException()

        # Make sure we respect page boundaries in case the chip and jlink
        # implementation will only work correctly when writing entire pages.
        address, binary = self._align_and_stretch_to_page(address, binary)

        commands = [
            "h\nr",
            "loadbin {{binary}}, {address:#x}".format(address=address),
            "verifybin {{binary}}, {address:#x}".format(address=address),
            "r\nh\ng\nq",
        ]

        self._run_jtag_commands(commands, binary)

    def read_range(self, address, length):
        if self.address_maximum and address > self.address_maximum:
            raise ChannelAddressErrorException()

        commands = []
        if self.jlink_device == "cortex-m0":
            # We are in generic mode, trying to read attributes.
            # We've found that when connecting to a generic
            # `cortex-m0` reset commands sometimes fail, however it
            # seems that reading the binary directly from flash
            # still works, so do that.
            commands = [
                "savebin {{binary}}, {address:#x} {length}".format(
                    address=address, length=length
                ),
                "\nq",
            ]
        else:
            # We already know the specific jtag device we are
            # connected to. This means we can reset and run code.
            commands = [
                "h\nr",
                "savebin {{binary}}, {address:#x} {length}".format(
                    address=address, length=length
                ),
                "r\nh\ng\nq",
            ]

        # Always return a valid byte array (like the serial version does)
        read = bytes()
        result = self._run_jtag_commands(commands, None, write=False)
        if result:
            read += result

        # Check to make sure we didn't get too many
        if len(read) > length:
            read = read[0:length]

        return read

    def clear_bytes(self, address):
        if self.address_maximum and address > self.address_maximum:
            raise ChannelAddressErrorException()

        logging.debug("Clearing 512 bytes starting at address {:#0x}".format(address))

        # Write 512 bytes of 0xFF as that seems to work.
        binary = bytes([0xFF] * 512)
        commands = [
            "h\nr",
            "loadbin {{binary}}, {address:#x}".format(address=address),
            "verifybin {{binary}}, {address:#x}".format(address=address),
            "r\nh\ng\nq",
        ]

        self._run_jtag_commands(commands, binary)

    def determine_current_board(self):
        if self.board and self.arch and self.jlink_device and self.page_size > 0:
            # These are already set! Yay we are done.
            return

        # If we get to here, we still have unknown settings and we need to
        # retrieve them from the board itself. If they exist, they will be
        # stored as attributes in the flash of the board.
        attributes = self.get_all_attributes()
        for attribute in attributes:
            if attribute and attribute["key"] == "board" and self.board == None:
                self.board = attribute["value"]
            if attribute and attribute["key"] == "arch" and self.arch == None:
                self.arch = attribute["value"]
            if attribute and attribute["key"] == "jldevice":
                self.jlink_device = attribute["value"]
            if attribute and attribute["key"] == "pagesize" and self.page_size == 0:
                self.page_size = attribute["value"]

        # We might need to fill in if we only got a "board" attribute.
        self._configure_from_known_boards()

        # Check that we learned what we needed to learn.
        if (
            self.board == None
            or self.arch == None
            or self.jlink_device == "cortex-m0"
            or self.page_size == 0
        ):
            raise TockLoaderException(
                "Could not determine the current board or arch or jtag device name"
            )

    def run_terminal(self):
        """
        Use JLinkRTTClient to listen for RTT messages.
        """
        if getattr(self, "jlink_device", None) == None:
            # Try to figure it out.
            self.open_link_to_board()

        # See if we succeeded.
        if getattr(self, "jlink_device", None) == None:
            logging.error(
                "Unknown jlink_device. Use the --board or --jlink-device options."
            )
            return

        logging.status("Starting JLinkExe JTAG connection.")
        jtag_p = subprocess.Popen(
            "{} -device {} -if {} -speed {} -autoconnect 1 -jtagconf -1,-1".format(
                self.jlink_cmd, self.jlink_device, self.jlink_if, self.jlink_speed
            ).split(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Delay to give the JLinkExe JTAG connection time to start before running
        # the RTT listener.
        time.sleep(1)

        # Determine the name of the RTT binary to run.
        self.jlink_rtt_cmd = getattr(self.args, "jlink_rtt_cmd")
        if self.jlink_rtt_cmd == None:
            self.jlink_rtt_cmd = "JLinkRTTClient"

        logging.status("Starting {} to listen for messages.".format(self.jlink_rtt_cmd))
        p = subprocess.Popen(
            "{}".format(self.jlink_rtt_cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for stdout_line in iter(p.stdout.readline, ""):
            l = stdout_line.decode("utf-8")
            if not l.startswith("###RTT Client: *"):
                print(l, end="")
