// SPDX-License-Identifier: GPL-2.0-or-later
// Write-only fail-safe helper; qcontrol is the sole PIC reader.
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <termios.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	struct termios t;
	char *end;
	unsigned long value;
	unsigned char byte;
	int fd, ret = 1;

	if (argc != 4 || strcmp(argv[2], "send"))
		return 2;
	errno = 0;
	value = strtoul(argv[3], &end, 0);
	if (errno || end == argv[3] || *end || value > 255)
		return 2;
	fd = open(argv[1], O_WRONLY | O_NOCTTY | O_CLOEXEC);
	if (fd < 0)
		return 1;
	if (tcgetattr(fd, &t))
		goto out;
	cfmakeraw(&t);
	t.c_cflag |= CLOCAL | CREAD;
	t.c_cflag &= ~(CRTSCTS | CSTOPB | PARENB);
	if (cfsetispeed(&t, B19200) || cfsetospeed(&t, B19200) ||
	    tcsetattr(fd, TCSANOW, &t))
		goto out;
	byte = value;
	if (write(fd, &byte, 1) == 1 && !tcdrain(fd))
		ret = 0;
out:
	if (close(fd))
		ret = 1;
	return ret;
}
