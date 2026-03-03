/*
 * Fix C: libdevkit-sdk-core-lib.a (StringUtils.o) was compiled with
 * __HAVE_LOCALE_INFO__ which makes __locale_ctype_ptr a function rather than
 * a macro.  GCC 14 / newlib-nano does not provide this function, so we stub
 * it to return the C-locale ctype table that is always present in libc_nano.
 */
extern const char _ctype_[];

const char *__locale_ctype_ptr(void)
{
    return _ctype_;
}
