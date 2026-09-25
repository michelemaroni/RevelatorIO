#include <alsa/asoundlib.h>
#include <alsa/control_external.h>
#include <errno.h>
#include <fcntl.h>
#include <json-c/json.h>
#include <math.h>
#include <poll.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#define IO24_REPLY_LIMIT 16384
#define IO24_TIMEOUT_MS 3000

enum value_kind {
    VALUE_PERCENT,
    VALUE_BLEND,
    VALUE_DB,
    VALUE_BOOLEAN,
    VALUE_FLAG,
    VALUE_MAIN_MUTE,
};

struct control_spec {
    const char *name;
    const char *field;
    const char *param;
    int channel;
    unsigned int flag;
    enum value_kind kind;
    long minimum;
    long maximum;
    bool writable;
};

static const struct control_spec controls[] = {
    {"Main Volume", "mainVolume", "mainvol", 0, 0, VALUE_PERCENT, 0, 100, true},
    {"Headphone Volume", "hpVolume", "hpvol", 0, 0, VALUE_PERCENT, 0, 100, true},
    {"Monitor Blend", "monitorMix", "blend", 0, 0, VALUE_BLEND, 0, 100, true},
    {"Mic/Inst Capture Gain (dB)", "input1Gain", "gain", 1, 0, VALUE_DB, 0, 60, true},
    {"Headset Capture Gain (dB)", "input2Gain", "gain", 2, 0, VALUE_DB, 0, 60, true},
    {"Mic/Inst Capture Phantom", "input1PhantomPower", "phantom", 1, 0, VALUE_BOOLEAN, 0, 1, true},
    {"Mic/Inst Capture Processing", "flags", "fxmix", 1, 1U << 5, VALUE_FLAG, 0, 1, true},
    {"Headset Capture Processing", "flags", "fxmix", 2, 1U << 6, VALUE_FLAG, 0, 1, true},
    {"Main Output Mute", "flags", NULL, 0, 1U << 1, VALUE_MAIN_MUTE, 0, 1, false},
};

static const size_t control_count = sizeof(controls) / sizeof(controls[0]);

struct io24_control {
    snd_ctl_ext_t ext;
};

static int socket_address(struct sockaddr_un *address)
{
    const char *path = getenv("IO24D_SOCKET");

    if (path == NULL || *path == '\0') {
        path = getenv("XDG_RUNTIME_DIR");
        if (path != NULL && *path != '\0') {
            if (strlen(path) + sizeof("/io24d.sock") > sizeof(address->sun_path))
                return -ENAMETOOLONG;
            strcpy(address->sun_path, path);
            strcat(address->sun_path, "/io24d.sock");
            return 0;
        }
        path = "/tmp/io24d.sock";
    }
    if (strlen(path) + 1 > sizeof(address->sun_path))
        return -ENAMETOOLONG;
    strcpy(address->sun_path, path);
    return 0;
}

static int wait_for_fd(int fd, short events)
{
    struct pollfd descriptor = {.fd = fd, .events = events};
    int result;

    do {
        result = poll(&descriptor, 1, IO24_TIMEOUT_MS);
    } while (result < 0 && errno == EINTR);
    if (result < 0)
        return -errno;
    if (result == 0)
        return -ETIMEDOUT;
    if (descriptor.revents & POLLIN)
        return 0;
    if (descriptor.revents & (POLLERR | POLLHUP | POLLNVAL))
        return -ECONNRESET;
    return 0;
}

static int send_all(int fd, const char *data, size_t length)
{
    size_t sent = 0;

    while (sent < length) {
        ssize_t result = send(fd, data + sent, length - sent, MSG_NOSIGNAL);
        if (result < 0) {
            if (errno == EINTR)
                continue;
            return -errno;
        }
        if (result == 0)
            return -EPIPE;
        sent += (size_t)result;
    }
    return 0;
}

static int receive_reply(int fd, char *reply, size_t capacity, size_t *length)
{
    size_t used = 0;

    while (used + 1 < capacity) {
        ssize_t result = recv(fd, reply + used, capacity - used - 1, 0);
        char *newline;

        if (result < 0) {
            if (errno == EINTR)
                continue;
            return -errno;
        }
        if (result == 0)
            return used == 0 ? -ECONNRESET : -EPROTO;
        used += (size_t)result;
        newline = memchr(reply, '\n', used);
        if (newline != NULL) {
            *length = (size_t)(newline - reply);
            *newline = '\0';
            return 0;
        }
    }
    return -EMSGSIZE;
}

static int parse_reply(const char *text, size_t length, struct json_object **reply)
{
    struct json_tokener *tokener = json_tokener_new();
    enum json_tokener_error error;
    struct json_object *object;
    int result = 0;

    if (tokener == NULL)
        return -ENOMEM;
    object = json_tokener_parse_ex(tokener, text, (int)length);
    error = json_tokener_get_error(tokener);
    if (error != json_tokener_success || object == NULL ||
        json_tokener_get_parse_end(tokener) != length) {
        SNDERR("io24 ALSA: malformed daemon response");
        if (object != NULL)
            json_object_put(object);
        result = -EPROTO;
    } else {
        *reply = object;
    }
    json_tokener_free(tokener);
    return result;
}

static int daemon_request(const char *request, struct json_object **reply)
{
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    const char *path;
    char buffer[IO24_REPLY_LIMIT];
    size_t request_length = strlen(request);
    size_t reply_length = 0;
    struct json_object *ok;
    int fd;
    int result;

    *reply = NULL;
    result = socket_address(&address);
    if (result < 0) {
        SNDERR("io24 ALSA: daemon socket path is too long");
        return result;
    }
    path = address.sun_path;
    fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0)
        return -errno;
    result = connect(fd, (struct sockaddr *)&address, sizeof(address));
    if (result < 0) {
        result = -errno;
        SNDERR("io24 ALSA: cannot connect to %s: %s", path, strerror(-result));
        close(fd);
        return result;
    }
    result = send_all(fd, request, request_length);
    if (result == 0)
        result = send_all(fd, "\n", 1);
    if (result == 0)
        result = wait_for_fd(fd, POLLIN);
    if (result == 0)
        result = receive_reply(fd, buffer, sizeof(buffer), &reply_length);
    close(fd);
    if (result < 0)
        return result;
    result = parse_reply(buffer, reply_length, reply);
    if (result < 0)
        return result;
    if (!json_object_object_get_ex(*reply, "ok", &ok) ||
        !json_object_is_type(ok, json_type_boolean)) {
        SNDERR("io24 ALSA: daemon response has no boolean ok field");
        result = -EPROTO;
        goto fail;
    }
    if (!json_object_get_boolean(ok)) {
        struct json_object *message;
        SNDERR("io24 ALSA: daemon rejected request: %s",
              json_object_object_get_ex(*reply, "error", &message) &&
                      json_object_is_type(message, json_type_string)
                  ? json_object_get_string(message)
                  : "unknown error");
        result = -EIO;
        goto fail;
    }
    return 0;

fail:
    json_object_put(*reply);
    *reply = NULL;
    return result;
}

static int response_state(struct json_object *reply, struct json_object **state)
{
    if (!json_object_object_get_ex(reply, "state", state) ||
        !json_object_is_type(*state, json_type_object)) {
        SNDERR("io24 ALSA: daemon response has no state object");
        return -EPROTO;
    }
    return 0;
}

static int read_state(struct json_object **state)
{
    struct json_object *reply;
    int result = daemon_request("{\"cmd\":\"status\"}", &reply);

    if (result < 0)
        return result;
    result = response_state(reply, state);
    if (result == 0)
        json_object_get(*state);
    json_object_put(reply);
    return result;
}

static int state_number(const struct control_spec *spec, struct json_object *state, double *number)
{
    struct json_object *value;

    if (!json_object_object_get_ex(state, spec->field, &value) ||
        (spec->kind == VALUE_BOOLEAN
             ? !json_object_is_type(value, json_type_boolean)
             : (!json_object_is_type(value, json_type_double) &&
                !json_object_is_type(value, json_type_int)))) {
        SNDERR("io24 ALSA: state has no usable %s field", spec->field);
        return -EPROTO;
    }
    *number = json_object_get_double(value);
    if (!isfinite(*number)) {
        SNDERR("io24 ALSA: state field %s is not finite", spec->field);
        return -EPROTO;
    }
    return 0;
}

static int value_from_state(const struct control_spec *spec, struct json_object *state, long *result)
{
    double number;
    long value;
    int status;

    if (spec->kind == VALUE_FLAG || spec->kind == VALUE_MAIN_MUTE) {
        struct json_object *flags;
        int64_t bits;

        if (!json_object_object_get_ex(state, spec->field, &flags) ||
            !json_object_is_type(flags, json_type_int)) {
            SNDERR("io24 ALSA: state has no integer %s field", spec->field);
            return -EPROTO;
        }
        bits = json_object_get_int64(flags);
        *result = (bits & spec->flag) != 0;
        if (spec->kind == VALUE_FLAG)
            *result = !*result;
        return 0;
    }
    status = state_number(spec, state, &number);
    if (status < 0)
        return status;
    switch (spec->kind) {
    case VALUE_PERCENT:
        value = lround(number * spec->maximum);
        break;
    case VALUE_BLEND:
        value = lround((number + 1.0) * spec->maximum / 2.0);
        break;
    case VALUE_DB:
        value = lround(number);
        break;
    case VALUE_BOOLEAN:
        value = number != 0.0;
        break;
    default:
        return -EINVAL;
    }
    if (value < spec->minimum || value > spec->maximum) {
        SNDERR("io24 ALSA: %s returned out-of-range value %ld", spec->name, value);
        return -EPROTO;
    }
    *result = value;
    return 0;
}

static double value_for_daemon(const struct control_spec *spec, long value)
{
    switch (spec->kind) {
    case VALUE_PERCENT:
        return (double)value / spec->maximum;
    case VALUE_BLEND:
        return (double)(value * 2 - spec->maximum) / spec->maximum;
    case VALUE_DB:
        return (double)value;
    case VALUE_BOOLEAN:
        return value != 0;
    case VALUE_FLAG:
        return value != 0;
    default:
        return 0.0;
    }
}

static int write_value(const struct control_spec *spec, long requested, long *actual)
{
    struct json_object *request = json_object_new_object();
    struct json_object *reply = NULL;
    struct json_object *state = NULL;
    struct json_object *cmd = json_object_new_string("set");
    struct json_object *param = spec->param == NULL ? NULL : json_object_new_string(spec->param);
    struct json_object *value = json_object_new_double(value_for_daemon(spec, requested));
    struct json_object *channel = NULL;
    char payload[256];
    int result;

    if (spec->channel != 0)
        channel = json_object_new_int(spec->channel);
    if (spec->param == NULL) {
        result = -EACCES;
        goto done;
    }
    if (request == NULL || cmd == NULL || param == NULL || value == NULL ||
        (spec->channel != 0 && channel == NULL)) {
        result = -ENOMEM;
        goto done;
    }
    json_object_object_add(request, "cmd", cmd);
    cmd = NULL;
    json_object_object_add(request, "param", param);
    param = NULL;
    if (channel != NULL) {
        json_object_object_add(request, "channel", channel);
        channel = NULL;
    }
    json_object_object_add(request, "value", value);
    value = NULL;
    if (snprintf(payload, sizeof(payload), "%s",
                 json_object_to_json_string_ext(request, JSON_C_TO_STRING_PLAIN)) >=
        (int)sizeof(payload)) {
        result = -EOVERFLOW;
        goto done;
    }
    result = daemon_request(payload, &reply);
    if (result == 0)
        result = response_state(reply, &state);
    if (result == 0)
        result = value_from_state(spec, state, actual);

done:
    if (cmd != NULL)
        json_object_put(cmd);
    if (param != NULL)
        json_object_put(param);
    if (value != NULL)
        json_object_put(value);
    if (channel != NULL)
        json_object_put(channel);
    if (request != NULL)
        json_object_put(request);
    if (reply != NULL)
        json_object_put(reply);
    return result;
}

static void io24_close(snd_ctl_ext_t *ext)
{
    free(ext->private_data);
}

static int io24_elem_count(snd_ctl_ext_t *ext)
{
    (void)ext;
    return (int)control_count;
}

static int io24_elem_list(snd_ctl_ext_t *ext, unsigned int offset, snd_ctl_elem_id_t *id)
{
    (void)ext;
    if (offset >= control_count)
        return -EINVAL;
    snd_ctl_elem_id_clear(id);
    snd_ctl_elem_id_set_interface(id, SND_CTL_ELEM_IFACE_MIXER);
    snd_ctl_elem_id_set_name(id, controls[offset].name);
    return 0;
}

static snd_ctl_ext_key_t io24_find_elem(snd_ctl_ext_t *ext, const snd_ctl_elem_id_t *id)
{
    const char *name;
    size_t index;

    (void)ext;
    if (snd_ctl_elem_id_get_interface(id) != SND_CTL_ELEM_IFACE_MIXER)
        return SND_CTL_EXT_KEY_NOT_FOUND;
    name = snd_ctl_elem_id_get_name(id);
    if (name == NULL)
        return SND_CTL_EXT_KEY_NOT_FOUND;
    for (index = 0; index < control_count; index++) {
        if (strcmp(name, controls[index].name) == 0)
            return (snd_ctl_ext_key_t)index;
    }
    return SND_CTL_EXT_KEY_NOT_FOUND;
}

static int io24_get_attribute(snd_ctl_ext_t *ext, snd_ctl_ext_key_t key, int *type,
                              unsigned int *access, unsigned int *count)
{
    const struct control_spec *spec;

    (void)ext;
    if (key >= control_count)
        return -EINVAL;
    spec = &controls[key];
    *type = SND_CTL_ELEM_TYPE_INTEGER;
    *access = spec->writable ? SND_CTL_EXT_ACCESS_READWRITE : SND_CTL_EXT_ACCESS_READ;
    *access |= SND_CTL_EXT_ACCESS_VOLATILE;
    *count = 1;
    return 0;
}

static int io24_get_integer_info(snd_ctl_ext_t *ext, snd_ctl_ext_key_t key, long *minimum,
                                 long *maximum, long *step)
{
    (void)ext;
    if (key >= control_count)
        return -EINVAL;
    *minimum = controls[key].minimum;
    *maximum = controls[key].maximum;
    *step = 1;
    return 0;
}

static int io24_read_integer(snd_ctl_ext_t *ext, snd_ctl_ext_key_t key, long *value)
{
    struct json_object *state;
    int result;

    (void)ext;
    if (key >= control_count)
        return -EINVAL;
    result = read_state(&state);
    if (result == 0) {
        result = value_from_state(&controls[key], state, value);
        json_object_put(state);
    }
    return result;
}

static int io24_write_integer(snd_ctl_ext_t *ext, snd_ctl_ext_key_t key, long *value)
{
    const struct control_spec *spec;
    long current;
    int result;

    (void)ext;
    if (key >= control_count)
        return -EINVAL;
    spec = &controls[key];
    if (!spec->writable)
        return -EACCES;
    if (*value < spec->minimum || *value > spec->maximum)
        return -ERANGE;
    result = io24_read_integer(ext, key, &current);
    if (result < 0)
        return result;
    if (current == *value)
        return 0;
    result = write_value(spec, *value, value);
    return result < 0 ? result : 1;
}

static const snd_ctl_ext_callback_t callbacks = {
    .close = io24_close,
    .elem_count = io24_elem_count,
    .elem_list = io24_elem_list,
    .find_elem = io24_find_elem,
    .get_attribute = io24_get_attribute,
    .get_integer_info = io24_get_integer_info,
    .read_integer = io24_read_integer,
    .write_integer = io24_write_integer,
};

SND_CTL_PLUGIN_DEFINE_FUNC(io24)
{
    struct io24_control *io24 = calloc(1, sizeof(*io24));
    int result;

    (void)root;
    (void)conf;
    if (io24 == NULL)
        return -ENOMEM;
    io24->ext.version = SND_CTL_EXT_VERSION;
    io24->ext.card_idx = -1;
    strcpy(io24->ext.id, "IO24");
    strcpy(io24->ext.driver, "io24");
    strcpy(io24->ext.name, "PreSonus IO 24 / IO 44");
    strcpy(io24->ext.longname, "PreSonus Revelator IO 24 / IO 44 via io24d");
    strcpy(io24->ext.mixername, "PreSonus Revelator IO 24 / IO 44");
    io24->ext.poll_fd = -1;
    io24->ext.callback = &callbacks;
    io24->ext.private_data = io24;
    result = snd_ctl_ext_create(&io24->ext, name, mode);
    if (result < 0) {
        free(io24);
        return result;
    }
    *handlep = io24->ext.handle;
    return 0;
}

SND_CTL_PLUGIN_SYMBOL(io24);
