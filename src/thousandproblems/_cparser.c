/*
 * C port of thousandproblems._pyparser: the same lexer and recursive-descent parser, building
 * the same node objects. Keep the two in step; tests/test_backends.py checks that they
 * agree on trees and on error messages and positions.
 *
 * Differences that are deliberate:
 *   - Nodes are allocated and filled in directly, without running the dataclass
 *     __init__. The instances are of the very same classes.
 *   - Nesting that would use more than STACK_BUDGET bytes of C stack, or text that
 *     cannot be encoded as UTF-8 (lone surrogates), makes parse() return
 *     NotImplemented, and the caller falls back to the Python parser, which has no
 *     such limits.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* How much C stack the parser may use before handing the text to the Python parser.
 * Measured in bytes of real stack rather than in levels of nesting, because the size of
 * a frame depends on the compiler's inlining. Threads can have stacks as small as
 * 256 KB (macOS default is 512 KB), so this leaves a wide margin. */
#define STACK_BUDGET (160 * 1024)

/* ------------------------------------------------------------------------------------
 * Token kinds
 * ---------------------------------------------------------------------------------- */

enum {
    K_EOF = 0, K_SQ, K_BQ, K_DISTINCT, K_REAL, K_RATIONAL, K_INTEGER, K_SYSTEM, K_DEFINED,
    K_UPPER, K_LOWER,
    /* operators, in the same order as _OPERATORS in _pyparser.py */
    O_IFF, O_XOR, O_SEQUENT, O_ATAT_PLUS, O_ATAT_MINUS, O_DIAMOND, O_BOX, O_BRACE_DOT,
    O_PAREN_DOT,
    O_IMPLIES, O_IMPLIED_BY, O_SUBTYPE, O_NOR, O_NAND, O_NEQ, O_BANGBANG, O_TYPE_FORALL,
    O_QQ, O_TYPE_EXISTS, O_CHOICE, O_DESCRIPTION, O_AT_EQ, O_ASSIGN, O_DEFEQ,
    O_LPAREN, O_RPAREN, O_LBRACK, O_RBRACK, O_LBRACE, O_RBRACE, O_COMMA, O_DOT, O_COLON,
    O_OR, O_AND, O_NOT, O_FORALL, O_EXISTS, O_EQ, O_AT, O_GT, O_STAR, O_PLUS, O_MINUS,
    O_HASH, O_CARET,
    NKINDS
};

typedef struct {
    const char *text;
    int kind;
} OpDef;

static const OpDef OPS3[] = {
    {"<=>", O_IFF}, {"<~>", O_XOR}, {"-->", O_SEQUENT}, {"@@+", O_ATAT_PLUS},
    {"@@-", O_ATAT_MINUS}, {"<.>", O_DIAMOND}, {"[.]", O_BOX}, {"{.}", O_BRACE_DOT},
    {"(.)", O_PAREN_DOT},
};
static const OpDef OPS2[] = {
    {"=>", O_IMPLIES}, {"<=", O_IMPLIED_BY}, {"<<", O_SUBTYPE}, {"~|", O_NOR},
    {"~&", O_NAND}, {"!=", O_NEQ}, {"!!", O_BANGBANG}, {"!>", O_TYPE_FORALL},
    {"??", O_QQ}, {"?*", O_TYPE_EXISTS}, {"@+", O_CHOICE}, {"@-", O_DESCRIPTION},
    {"@=", O_AT_EQ}, {":=", O_ASSIGN}, {"==", O_DEFEQ},
};
static const OpDef OPS1[] = {
    {"(", O_LPAREN}, {")", O_RPAREN}, {"[", O_LBRACK}, {"]", O_RBRACK}, {"{", O_LBRACE},
    {"}", O_RBRACE}, {",", O_COMMA}, {".", O_DOT}, {":", O_COLON}, {"|", O_OR},
    {"&", O_AND}, {"~", O_NOT}, {"!", O_FORALL}, {"?", O_EXISTS}, {"=", O_EQ},
    {"@", O_AT}, {">", O_GT}, {"*", O_STAR}, {"+", O_PLUS}, {"-", O_MINUS},
    {"#", O_HASH}, {"^", O_CARET},
};
#define COUNT(a) ((int)(sizeof(a) / sizeof((a)[0])))

static const char *OP_TEXT[NKINDS];
static signed char ONE_CHAR[256];

typedef enum { L_FOF, L_TFF, L_THF } Lang;
enum { R_THF, R_TFF, R_TCF, R_FOF, R_CNF, R_TPI, NRULES };
static const char *const RULE_NAMES[NRULES] = {"thf", "tff", "tcf", "fof", "cnf", "tpi"};

#define IS_NUM(k) ((k) == K_INTEGER || (k) == K_RATIONAL || (k) == K_REAL)
#define IS_WORD(k) ((k) == K_LOWER || (k) == K_SQ || (k) == K_BQ)

/* ------------------------------------------------------------------------------------
 * Module state: the node classes, interned field and operator names
 * ---------------------------------------------------------------------------------- */

#define CLASS_LIST(X)                                                                      \
    X(Variable) X(Function) X(Number) X(DistinctObject) X(Tuple) X(Unary) X(Binary)        \
    X(NAry) X(TypedVariable) X(Quantified) X(Typing) X(ConnectiveTerm) X(Index)            \
    X(LongConnective) X(ModalAtom) X(Let) X(FormulaData) X(Name) X(Parent) X(Inference)    \
    X(Introduced) X(FileSource) X(SourceList) X(Annotations) X(AnnotatedFormula)           \
    X(Include) X(Comment) X(TPTPFile) X(Not) X(Box) X(Diamond) X(Or) X(And) X(Apply)       \
    X(Product) X(Sum) X(Implies) X(ImpliedBy) X(Iff) X(Xor) X(Nor) X(Nand) X(Equals)       \
    X(NotEquals) X(Arrow) X(Definition) X(Assignment) X(Sequent) X(Subtype) X(Colon)       \
    X(ForAll) X(Exists) X(Lambda) X(Choice) X(Description) X(TypeForAll) X(TypeExists)  \
    X(Symbol)

#define FIELD_LIST(X)                                                                      \
    X(name) X(args) X(text) X(op) X(arg) X(left) X(right) X(items) X(type) X(quantifier)   \
    X(variables) X(body) X(atom) X(term) X(params) X(connective) X(types) X(definitions)   \
    X(language) X(formula) X(source) X(details) X(rule) X(info) X(parents) X(kind)         \
    X(file) X(sources) X(annotations) X(subrole) X(line) X(role) X(selection) X(space)     \
    X(inputs) X(comments) X(filename) X(index) X(arity) X(symbols)

typedef struct {
    PyObject *error_at;
    PyObject *star;
#define X(n) PyObject *c_##n;
    CLASS_LIST(X)
#undef X
#define X(n) PyObject *n_##n;
    FIELD_LIST(X)
#undef X
    PyObject *opstr[NKINDS];
    PyObject *cls_of_op[NKINDS]; /* borrowed from c_* */
    PyObject *lang_str[NRULES];
} State;

static int
state_traverse(PyObject *m, visitproc visit, void *arg)
{
    State *st = (State *)PyModule_GetState(m);
    Py_VISIT(st->error_at);
    Py_VISIT(st->star);
#define X(n) Py_VISIT(st->c_##n);
    CLASS_LIST(X)
#undef X
#define X(n) Py_VISIT(st->n_##n);
    FIELD_LIST(X)
#undef X
    for (int i = 0; i < NKINDS; i++) Py_VISIT(st->opstr[i]);
    for (int i = 0; i < NRULES; i++) Py_VISIT(st->lang_str[i]);
    return 0;
}

static int
state_clear(PyObject *m)
{
    State *st = (State *)PyModule_GetState(m);
    Py_CLEAR(st->error_at);
    Py_CLEAR(st->star);
#define X(n) Py_CLEAR(st->c_##n);
    CLASS_LIST(X)
#undef X
#define X(n) Py_CLEAR(st->n_##n);
    FIELD_LIST(X)
#undef X
    for (int i = 0; i < NKINDS; i++) {
        Py_CLEAR(st->opstr[i]);
        st->cls_of_op[i] = NULL;
    }
    for (int i = 0; i < NRULES; i++) Py_CLEAR(st->lang_str[i]);
    return 0;
}

static void
state_free(void *m)
{
    state_clear((PyObject *)m);
}

/* ------------------------------------------------------------------------------------
 * Growable arrays of owned references
 * ---------------------------------------------------------------------------------- */

typedef struct {
    PyObject **v;
    Py_ssize_t n, cap;
} Vec;

/* Takes ownership of `o` whether or not it succeeds. A NULL `o` (a failed parse
 * upstream, exception already set) just reports failure. */
static int
vec_push_new(Vec *a, PyObject *o)
{
    if (o == NULL) return -1;
    if (a->n == a->cap) {
        Py_ssize_t cap = a->cap ? a->cap * 2 : 4;
        PyObject **v = (PyObject **)PyMem_Realloc(a->v, (size_t)cap * sizeof(PyObject *));
        if (v == NULL) {
            Py_DECREF(o);
            PyErr_NoMemory();
            return -1;
        }
        a->v = v;
        a->cap = cap;
    }
    a->v[a->n++] = o;
    return 0;
}

static PyObject *
vec_tuple(Vec *a)
{
    PyObject *t = PyTuple_New(a->n);
    if (t == NULL) return NULL;
    for (Py_ssize_t i = 0; i < a->n; i++) PyTuple_SET_ITEM(t, i, a->v[i]);
    a->n = 0;
    return t;
}

static void
vec_free(Vec *a)
{
    for (Py_ssize_t i = 0; i < a->n; i++) Py_DECREF(a->v[i]);
    PyMem_Free(a->v);
    a->v = NULL;
    a->n = a->cap = 0;
}

/* ------------------------------------------------------------------------------------
 * Parser state
 * ---------------------------------------------------------------------------------- */

typedef struct {
    Py_ssize_t start, end, line;
    int kind;
} Tok;

/* ------------------------------------------------------------------------------------
 * A small hash table from (name, arity) to an id, with the entries kept in the order they
 * were added. It is used for the variables of one formula (arity 0) and for the symbols of
 * a file. Keys are not copied: they point into memory that outlives the table.
 * ---------------------------------------------------------------------------------- */

typedef struct {
    const char *key;
    Py_ssize_t klen, arity;
    Py_ssize_t val;    /* variables: index of the binding in scope, 0 for none */
    Py_ssize_t stamp;  /* symbols: number of the last formula that used it */
    PyObject *obj;     /* symbols: the Symbol */
    uint64_t hash;
} HEnt;

typedef struct {
    HEnt *ent;
    Py_ssize_t n, cap;
    Py_ssize_t *slot; /* open addressing; entry ids, -1 for empty */
    Py_ssize_t nslot; /* a power of two, or 0 */
} HTab;

static uint64_t
hash_key(const char *k, Py_ssize_t len, Py_ssize_t arity)
{
    uint64_t h = 1469598103934665603ULL ^ ((uint64_t)arity * 0x9E3779B97F4A7C15ULL);
    for (Py_ssize_t i = 0; i < len; i++) {
        h ^= (unsigned char)k[i];
        h *= 1099511628211ULL;
    }
    return h ^ (h >> 32);
}

static int
htab_rehash(HTab *t, Py_ssize_t nslot)
{
    Py_ssize_t *slot = (Py_ssize_t *)PyMem_Malloc((size_t)nslot * sizeof(Py_ssize_t));
    if (slot == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    for (Py_ssize_t i = 0; i < nslot; i++) slot[i] = -1;
    for (Py_ssize_t id = 0; id < t->n; id++) {
        Py_ssize_t i = (Py_ssize_t)(t->ent[id].hash & (uint64_t)(nslot - 1));
        while (slot[i] >= 0) i = (i + 1) & (nslot - 1);
        slot[i] = id;
    }
    PyMem_Free(t->slot);
    t->slot = slot;
    t->nslot = nslot;
    return 0;
}

/* The id of (key, arity), adding it if it is new (*added tells which). -1 on failure. */
static Py_ssize_t
htab_intern(HTab *t, const char *key, Py_ssize_t klen, Py_ssize_t arity, int *added)
{
    uint64_t h = hash_key(key, klen, arity);
    if (t->nslot == 0 || (t->n + 1) * 2 > t->nslot) {
        if (htab_rehash(t, t->nslot ? t->nslot * 2 : 64) < 0) return -1;
    }
    Py_ssize_t mask = t->nslot - 1, i = (Py_ssize_t)(h & (uint64_t)mask);
    for (;;) {
        Py_ssize_t id = t->slot[i];
        if (id < 0) break;
        HEnt *e = &t->ent[id];
        if (e->hash == h && e->klen == klen && e->arity == arity &&
            memcmp(e->key, key, (size_t)klen) == 0) {
            *added = 0;
            return id;
        }
        i = (i + 1) & mask;
    }
    if (t->n == t->cap) {
        Py_ssize_t cap = t->cap ? t->cap * 2 : 16;
        HEnt *ent = (HEnt *)PyMem_Realloc(t->ent, (size_t)cap * sizeof(HEnt));
        if (ent == NULL) {
            PyErr_NoMemory();
            return -1;
        }
        t->ent = ent;
        t->cap = cap;
    }
    HEnt *e = &t->ent[t->n];
    e->key = key;
    e->klen = klen;
    e->arity = arity;
    e->val = 0;
    e->stamp = 0;
    e->obj = NULL;
    e->hash = h;
    t->slot[i] = t->n;
    *added = 1;
    return t->n++;
}

/* Forget every entry, keeping a small table for reuse but not a huge one. */
static void
htab_reset(HTab *t)
{
    t->n = 0;
    if (t->nslot > 4096) {
        PyMem_Free(t->slot);
        PyMem_Free(t->ent);
        t->slot = NULL;
        t->ent = NULL;
        t->nslot = t->cap = 0;
    }
    else {
        for (Py_ssize_t i = 0; i < t->nslot; i++) t->slot[i] = -1;
    }
}

static void
htab_free(HTab *t)
{
    for (Py_ssize_t id = 0; id < t->n; id++) Py_XDECREF(t->ent[id].obj);
    PyMem_Free(t->slot);
    PyMem_Free(t->ent);
    memset(t, 0, sizeof *t);
}

typedef struct {
    Py_ssize_t id, prev; /* a variable's entry, and the binding it shadowed */
} Undo;

typedef struct {
    PyObject *name; /* NULL until the arguments are read */
    Py_ssize_t arity;
} Ev;

typedef struct {
    State *st;
    PyObject *text;     /* the str being parsed (borrowed) */
    PyObject *filename; /* borrowed */
    const char *s;      /* UTF-8 of `text` */
    Py_ssize_t n;
    Tok *toks;
    Py_ssize_t ntok, cap, i;
    uintptr_t stack_base;
    int too_deep;
    Vec comments;
    /* variable numbering and the symbols of the formula being parsed */
    int numbering;      /* on inside a formula, off in annotations */
    int type_depth;     /* > 0 while reading a THF type, whose names are not symbols */
    Py_ssize_t count;   /* variables numbered so far in this formula */
    Py_ssize_t formula_no;
    HTab vars;
    Undo *undo;
    Py_ssize_t undo_n, undo_cap;
    Ev *events;
    Py_ssize_t ev_n, ev_cap;
    HTab symtab;        /* every symbol of the file, in order of first appearance */
} Parser;

#define CUR(p) (&(p)->toks[(p)->i])
#define KIND(p) ((p)->toks[(p)->i].kind)

static inline const Tok *
PEEK(Parser *p, Py_ssize_t off)
{
    Py_ssize_t j = p->i + off;
    if (j >= p->ntok) j = p->ntok - 1;
    return &p->toks[j];
}

#define ISA(obj, cls) PyObject_TypeCheck((obj), (PyTypeObject *)(cls))
#define TEXT_IS(p, t, lit)                                                                 \
    ((t)->end - (t)->start == (Py_ssize_t)(sizeof(lit) - 1) &&                              \
     memcmp((p)->s + (t)->start, (lit), sizeof(lit) - 1) == 0)

static PyObject *
tok_text(Parser *p, const Tok *t)
{
    return PyUnicode_FromStringAndSize(p->s + t->start, t->end - t->start);
}

static PyObject *
describe(Parser *p, const Tok *t)
{
    if (t->kind == K_EOF) return PyUnicode_FromString("end of input");
    PyObject *s = tok_text(p, t);
    if (s == NULL) return NULL;
    PyObject *r = PyObject_Repr(s);
    Py_DECREF(s);
    return r;
}

/* 1-based column counted in characters, as the Python parser reports it. */
static Py_ssize_t
column_of(Parser *p, Py_ssize_t pos)
{
    Py_ssize_t ls = pos;
    while (ls > 0 && p->s[ls - 1] != '\n') ls--;
    Py_ssize_t col = 1;
    for (Py_ssize_t k = ls; k < pos; k++) {
        if (((unsigned char)p->s[k] & 0xC0) != 0x80) col++;
    }
    return col;
}

/* Raise ParseError (built by the Python-side _error_at) at token `t`. Takes ownership
 * of `msg`. Always returns NULL so callers can `return fail(...)`. */
static PyObject *
raise_at(Parser *p, PyObject *msg, Py_ssize_t line, Py_ssize_t pos)
{
    if (msg == NULL) return NULL;
    PyObject *pyline = PyLong_FromSsize_t(line);
    PyObject *pycol = PyLong_FromSsize_t(column_of(p, pos));
    PyObject *exc = NULL;
    if (pyline && pycol) {
        exc = PyObject_CallFunctionObjArgs(p->st->error_at, msg, p->text, pyline, pycol,
                                           p->filename, NULL);
    }
    Py_DECREF(msg);
    Py_XDECREF(pyline);
    Py_XDECREF(pycol);
    if (exc != NULL) {
        PyErr_SetObject((PyObject *)Py_TYPE(exc), exc);
        Py_DECREF(exc);
    }
    return NULL;
}

/* A message with printf-style %s arguments, reported at token `at`. */
static PyObject *
fail_at(Parser *p, const Tok *at, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    PyObject *msg = PyUnicode_FromFormatV(fmt, ap);
    va_end(ap);
    return raise_at(p, msg, at->line, at->start);
}

/* `fmt` contains one %U, replaced by the description of token `found`. */
static PyObject *
fail_desc(Parser *p, const Tok *at, const Tok *found, const char *fmt)
{
    PyObject *d = describe(p, found);
    if (d == NULL) return NULL;
    PyObject *msg = PyUnicode_FromFormat(fmt, d);
    Py_DECREF(d);
    return raise_at(p, msg, at->line, at->start);
}

static PyObject *
fail_expected(Parser *p, const char *what)
{
    char fmt[96];
    snprintf(fmt, sizeof fmt, "expected %s, found %%U", what);
    return fail_desc(p, CUR(p), CUR(p), fmt);
}

static PyObject *
fail_term(Parser *p, const Tok *start)
{
    return fail_desc(p, start, start, "expected a formula, found the term %U");
}

static inline int
stack_exhausted(Parser *p)
{
    volatile char probe = 0;
    uintptr_t here = (uintptr_t)&probe;
    uintptr_t used = here > p->stack_base ? here - p->stack_base : p->stack_base - here;
    return used > STACK_BUDGET;
}

static PyObject *
too_deep(Parser *p)
{
    p->too_deep = 1;
    PyErr_SetNone(PyExc_RecursionError);
    return NULL;
}

static int
accept(Parser *p, int kind)
{
    if (KIND(p) == kind) {
        p->i++;
        return 1;
    }
    return 0;
}

static int
expect(Parser *p, int kind)
{
    if (KIND(p) != kind) {
        char what[16];
        snprintf(what, sizeof what, "'%s'", OP_TEXT[kind]);
        fail_expected(p, what);
        return -1;
    }
    p->i++;
    return 0;
}

#define EXPECT(kind)                                                                       \
    do {                                                                                   \
        if (expect(p, (kind)) < 0) goto done;                                              \
    } while (0)

/* ------------------------------------------------------------------------------------
 * Node construction
 * ---------------------------------------------------------------------------------- */

/* Allocate an instance of `cls` and fill its slots from (name, value) pairs. All values
 * are borrowed. A NULL value means a parse failed upstream; its exception is already
 * set, so just report failure. */
static PyObject *
mk(PyObject *cls, int n, ...)
{
    PyObject *names[8], *vals[8];
    va_list ap;
    va_start(ap, n);
    for (int i = 0; i < n; i++) {
        names[i] = va_arg(ap, PyObject *);
        vals[i] = va_arg(ap, PyObject *);
    }
    va_end(ap);
    for (int i = 0; i < n; i++) {
        if (vals[i] == NULL) return NULL;
    }
    PyObject *obj = PyType_GenericAlloc((PyTypeObject *)cls, 0);
    if (obj == NULL) return NULL;
    for (int i = 0; i < n; i++) {
        if (PyObject_GenericSetAttr(obj, names[i], vals[i]) < 0) {
            Py_DECREF(obj);
            return NULL;
        }
    }
    return obj;
}

#define S(p) ((p)->st)

static PyObject *
mk_word_node(Parser *p, PyObject *cls, PyObject *field, const Tok *t)
{
    PyObject *text = tok_text(p, t);
    if (text == NULL) return NULL;
    PyObject *r = mk(cls, 1, field, text);
    Py_DECREF(text);
    return r;
}

static PyObject *
mk_unary(Parser *p, int op, PyObject *arg)
{
    State *st = S(p);
    return mk(st->cls_of_op[op], 2, st->n_op, st->opstr[op], st->n_arg, arg);
}

static PyObject *
mk_binary(Parser *p, int op, PyObject *left, PyObject *right)
{
    State *st = S(p);
    return mk(st->cls_of_op[op], 3, st->n_op, st->opstr[op], st->n_left, left, st->n_right,
              right);
}

/* Consumes the contents of `args`. */
static PyObject *
mk_nary(Parser *p, int op, Vec *args)
{
    State *st = S(p);
    PyObject *t = vec_tuple(args);
    if (t == NULL) return NULL;
    PyObject *r = mk(st->cls_of_op[op], 2, st->n_op, st->opstr[op], st->n_args, t);
    Py_DECREF(t);
    return r;
}

static PyObject *
mk_quantified(Parser *p, int op, PyObject *variables, PyObject *body)
{
    State *st = S(p);
    return mk(st->cls_of_op[op], 3, st->n_quantifier, st->opstr[op], st->n_variables,
              variables, st->n_body, body);
}

/* Function(name, args): `args` may be NULL for a constant. Borrowed. */
static PyObject *
mk_function(Parser *p, PyObject *name, PyObject *args)
{
    State *st = S(p);
    if (name == NULL) return NULL;
    if (args == NULL) args = PyTuple_New(0);
    else Py_INCREF(args);
    if (args == NULL) return NULL;
    PyObject *r = mk(st->c_Function, 2, st->n_name, name, st->n_args, args);
    Py_DECREF(args);
    return r;
}

static PyObject *
mk_tuple_node(Parser *p, Vec *items)
{
    State *st = S(p);
    PyObject *t = vec_tuple(items);
    if (t == NULL) return NULL;
    PyObject *r = mk(st->c_Tuple, 1, st->n_items, t);
    Py_DECREF(t);
    return r;
}

/* ------------------------------------------------------------------------------------
 * Variable numbering and symbols. These mirror begin_formula and friends in _pyparser.py.
 * ---------------------------------------------------------------------------------- */

static void
ev_clear(Parser *p)
{
    for (Py_ssize_t i = 0; i < p->ev_n; i++) Py_XDECREF(p->events[i].name);
    p->ev_n = 0;
}

static void
begin_formula(Parser *p)
{
    p->numbering = 1;
    p->count = 0;
    p->type_depth = 0;
    p->formula_no++;
    htab_reset(&p->vars);
    p->undo_n = 0;
    ev_clear(p);
}

/* A quantifier binds a new variable. Returns its index, or -1 on failure. */
static Py_ssize_t
var_bind(Parser *p, const Tok *t)
{
    if (!p->numbering) return 0;
    int added;
    Py_ssize_t id = htab_intern(&p->vars, p->s + t->start, t->end - t->start, 0, &added);
    if (id < 0) return -1;
    if (p->undo_n == p->undo_cap) {
        Py_ssize_t cap = p->undo_cap ? p->undo_cap * 2 : 16;
        Undo *u = (Undo *)PyMem_Realloc(p->undo, (size_t)cap * sizeof(Undo));
        if (u == NULL) {
            PyErr_NoMemory();
            return -1;
        }
        p->undo = u;
        p->undo_cap = cap;
    }
    HEnt *e = &p->vars.ent[id];
    p->undo[p->undo_n].id = id;
    p->undo[p->undo_n].prev = e->val;
    p->undo_n++;
    e->val = ++p->count;
    return e->val;
}

/* The index of the variable in scope with this name; one not bound is numbered on first use. */
static Py_ssize_t
var_use(Parser *p, const Tok *t)
{
    if (!p->numbering) return 0;
    int added;
    Py_ssize_t id = htab_intern(&p->vars, p->s + t->start, t->end - t->start, 0, &added);
    if (id < 0) return -1;
    HEnt *e = &p->vars.ent[id];
    if (e->val == 0) e->val = ++p->count;
    return e->val;
}

/* A quantifier's scope has ended: restore the bindings its variables shadowed. */
static void
var_release(Parser *p, Py_ssize_t mark)
{
    while (p->undo_n > mark) {
        p->undo_n--;
        p->vars.ent[p->undo[p->undo_n].id].val = p->undo[p->undo_n].prev;
    }
}

/* Variable(name, index) or TypedVariable(name, type, index). `type` may be NULL. */
static PyObject *
mk_variable(Parser *p, const Tok *t, Py_ssize_t index, PyObject *type)
{
    State *st = S(p);
    PyObject *text = tok_text(p, t), *idx = PyLong_FromSsize_t(index), *r = NULL;
    if (text && idx) {
        if (type == NULL) {
            r = mk(st->c_Variable, 2, st->n_name, text, st->n_index, idx);
        }
        else {
            r = mk(st->c_TypedVariable, 3, st->n_name, text, st->n_type, type, st->n_index, idx);
        }
    }
    Py_XDECREF(text);
    Py_XDECREF(idx);
    return r;
}

static PyObject *
mk_variable_use(Parser *p, const Tok *t)
{
    Py_ssize_t index = var_use(p, t);
    if (index < 0) return NULL;
    return mk_variable(p, t, index, NULL);
}

/* Reserve the place in the order of first appearance of a function being read; the arity is
 * filled in once its arguments are. Returns the slot, or -1 on failure. */
static Py_ssize_t
ev_reserve(Parser *p)
{
    if (p->ev_n == p->ev_cap) {
        Py_ssize_t cap = p->ev_cap ? p->ev_cap * 2 : 32;
        Ev *ev = (Ev *)PyMem_Realloc(p->events, (size_t)cap * sizeof(Ev));
        if (ev == NULL) {
            PyErr_NoMemory();
            return -1;
        }
        p->events = ev;
        p->ev_cap = cap;
    }
    p->events[p->ev_n].name = NULL;
    p->events[p->ev_n].arity = 0;
    return p->ev_n++;
}

static void
ev_fill(Parser *p, Py_ssize_t slot, PyObject *name, Py_ssize_t arity)
{
    p->events[slot].name = Py_NewRef(name);
    p->events[slot].arity = arity;
}

/* The formula is finished: its symbols as a tuple, in order of first appearance, each added to
 * the file's table if new. */
static PyObject *
end_formula(Parser *p)
{
    State *st = S(p);
    Vec out = {0};
    PyObject *res = NULL;
    p->numbering = 0;
    for (Py_ssize_t i = 0; i < p->ev_n; i++) {
        Ev *ev = &p->events[i];
        if (ev->name == NULL) continue;
        Py_ssize_t klen;
        const char *key = PyUnicode_AsUTF8AndSize(ev->name, &klen);
        if (key == NULL) goto done;
        int added;
        Py_ssize_t id = htab_intern(&p->symtab, key, klen, ev->arity, &added);
        if (id < 0) goto done;
        HEnt *e = &p->symtab.ent[id];
        if (added) {
            /* The key points into ev->name, which the new Symbol keeps alive. */
            PyObject *arity = PyLong_FromSsize_t(ev->arity);
            if (arity == NULL) goto done;
            e->obj = mk(st->c_Symbol, 2, st->n_name, ev->name, st->n_arity, arity);
            Py_DECREF(arity);
            if (e->obj == NULL) goto done;
        }
        if (e->stamp != p->formula_no) {
            e->stamp = p->formula_no;
            if (vec_push_new(&out, Py_NewRef(e->obj)) < 0) goto done;
        }
    }
    res = vec_tuple(&out);
done:
    ev_clear(p);
    vec_free(&out);
    return res;
}

/* ------------------------------------------------------------------------------------
 * Lexer
 * ---------------------------------------------------------------------------------- */

static inline int
is_digit(int c)
{
    return c >= '0' && c <= '9';
}

static inline int
is_idchar(int c)
{
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
           c == '_';
}

static Py_ssize_t
scan_idchars(const char *s, Py_ssize_t n, Py_ssize_t i)
{
    while (i < n && is_idchar((unsigned char)s[i])) i++;
    return i;
}

/* A quoted item starting at s[pos] (the opening quote). Returns the offset past the
 * closing quote, or -1. At least one character is required. */
static Py_ssize_t
scan_quoted(const char *s, Py_ssize_t n, Py_ssize_t pos, char quote)
{
    Py_ssize_t i = pos + 1, count = 0;
    for (;;) {
        if (i >= n) return -1;
        unsigned char c = (unsigned char)s[i];
        if (c == (unsigned char)quote) return count ? i + 1 : -1;
        if (c == '\\') {
            if (i + 1 < n && (s[i + 1] == '\\' || s[i + 1] == quote)) {
                i += 2;
                count++;
                continue;
            }
            return -1;
        }
        if (c < 0x20 || c > 0x7E) return -1;
        i++;
        count++;
    }
}

/* Mirrors the real, rational and integer alternatives of the Python token regex, in
 * that order. Returns the end offset and sets *kind, or returns 0 if there is no
 * number at `pos`. */
static Py_ssize_t
scan_number(const char *s, Py_ssize_t n, Py_ssize_t pos, int *kind)
{
#define CH(j) ((j) < n ? (unsigned char)s[j] : 0)
    Py_ssize_t j = pos;
    if (s[j] == '+' || s[j] == '-') j++;
    if (!is_digit(CH(j))) return 0;

    Py_ssize_t k = j + 1; /* end of (?:0|[1-9][0-9]*) */
    if (s[j] != '0') {
        while (is_digit(CH(k))) k++;
    }

    if (CH(k) == '.' && is_digit(CH(k + 1))) {
        k++;
        while (is_digit(CH(k))) k++;
        if (CH(k) == 'e' || CH(k) == 'E') {
            Py_ssize_t m = k + 1;
            if (CH(m) == '+' || CH(m) == '-') m++;
            if (is_digit(CH(m))) {
                while (is_digit(CH(m))) m++;
                k = m;
            }
        }
        *kind = K_REAL;
        return k;
    }

    Py_ssize_t d = j;
    while (is_digit(CH(d))) d++;
    if (CH(d) == 'e' || CH(d) == 'E') {
        Py_ssize_t m = d + 1;
        if (CH(m) == '+' || CH(m) == '-') m++;
        if (is_digit(CH(m))) {
            while (is_digit(CH(m))) m++;
            *kind = K_REAL;
            return m;
        }
    }

    if (CH(k) == '/' && CH(k + 1) >= '1' && CH(k + 1) <= '9') {
        Py_ssize_t m = k + 2;
        while (is_digit(CH(m))) m++;
        *kind = K_RATIONAL;
        return m;
    }

    *kind = K_INTEGER;
    return k;
#undef CH
}

static int
push_tok(Parser *p, int kind, Py_ssize_t start, Py_ssize_t end, Py_ssize_t line)
{
    if (p->ntok == p->cap) {
        Py_ssize_t cap = p->cap * 2;
        Tok *t = (Tok *)PyMem_Realloc(p->toks, (size_t)cap * sizeof(Tok));
        if (t == NULL) {
            PyErr_NoMemory();
            return -1;
        }
        p->toks = t;
        p->cap = cap;
    }
    Tok *t = &p->toks[p->ntok++];
    t->kind = kind;
    t->start = start;
    t->end = end;
    t->line = line;
    return 0;
}

static int
push_comment(Parser *p, Py_ssize_t start, Py_ssize_t end, Py_ssize_t line)
{
    State *st = S(p);
    PyObject *text = PyUnicode_FromStringAndSize(p->s + start, end - start);
    PyObject *ln = PyLong_FromSsize_t(line);
    PyObject *c = NULL;
    if (text && ln) c = mk(st->c_Comment, 2, st->n_text, text, st->n_line, ln);
    Py_XDECREF(text);
    Py_XDECREF(ln);
    return vec_push_new(&p->comments, c);
}

static int
lex_error(Parser *p, Py_ssize_t pos, Py_ssize_t line)
{
    const char *s = p->s;
    unsigned char c = (unsigned char)s[pos];
    PyObject *msg;
    if (c == '\'') {
        msg = PyUnicode_FromString("unterminated or invalid single-quoted word");
    }
    else if (c == '"') {
        msg = PyUnicode_FromString("unterminated or invalid distinct object");
    }
    else if (c == '/' && pos + 1 < p->n && s[pos + 1] == '*') {
        msg = PyUnicode_FromString("unterminated block comment");
    }
    else {
        Py_ssize_t len = c < 0x80 ? 1 : c < 0xE0 ? 2 : c < 0xF0 ? 3 : 4;
        if (pos + len > p->n) len = p->n - pos;
        PyObject *ch = PyUnicode_FromStringAndSize(s + pos, len);
        if (ch == NULL) return -1;
        msg = PyUnicode_FromFormat("unexpected character %R", ch);
        Py_DECREF(ch);
    }
    raise_at(p, msg, line, pos);
    return -1;
}

static int
lex(Parser *p)
{
    const char *s = p->s;
    Py_ssize_t n = p->n, pos = 0, line = 1;
    p->cap = n / 3 + 16;
    p->toks = (Tok *)PyMem_Malloc((size_t)p->cap * sizeof(Tok));
    if (p->toks == NULL) {
        PyErr_NoMemory();
        return -1;
    }

    while (pos < n) {
        unsigned char c = (unsigned char)s[pos];
        Py_ssize_t end;
        int kind;

        switch (c) {
        case ' ': case '\t': case '\r': case '\n':
            end = pos;
            while (end < n && (s[end] == ' ' || s[end] == '\t' || s[end] == '\r' ||
                               s[end] == '\n')) {
                if (s[end] == '\n') line++;
                end++;
            }
            pos = end;
            continue;

        case '%':
            end = pos;
            while (end < n && s[end] != '\r' && s[end] != '\n') end++;
            if (push_comment(p, pos, end, line) < 0) return -1;
            pos = end;
            continue;

        case '/': {
            if (!(pos + 1 < n && s[pos + 1] == '*')) return lex_error(p, pos, line);
            end = pos + 2;
            while (end + 1 < n && !(s[end] == '*' && s[end + 1] == '/')) end++;
            if (end + 1 >= n) return lex_error(p, pos, line);
            end += 2;
            if (push_comment(p, pos, end, line) < 0) return -1;
            for (Py_ssize_t k = pos; k < end; k++) {
                if (s[k] == '\n') line++;
            }
            pos = end;
            continue;
        }

        case '\'':
            end = scan_quoted(s, n, pos, '\'');
            if (end < 0) return lex_error(p, pos, line);
            if (push_tok(p, K_SQ, pos, end, line) < 0) return -1;
            pos = end;
            continue;

        case '"':
            end = scan_quoted(s, n, pos, '"');
            if (end < 0) return lex_error(p, pos, line);
            if (push_tok(p, K_DISTINCT, pos, end, line) < 0) return -1;
            pos = end;
            continue;

        case '`':
            if (!(pos + 1 < n && s[pos + 1] >= 'A' && s[pos + 1] <= 'Z')) {
                return lex_error(p, pos, line);
            }
            end = scan_idchars(s, n, pos + 2);
            if (push_tok(p, K_BQ, pos, end, line) < 0) return -1;
            pos = end;
            continue;

        case '$':
            if (pos + 1 < n && s[pos + 1] == '$') {
                end = scan_idchars(s, n, pos + 2);
                kind = K_SYSTEM;
            }
            else {
                end = scan_idchars(s, n, pos + 1);
                kind = K_DEFINED;
            }
            if (push_tok(p, kind, pos, end, line) < 0) return -1;
            pos = end;
            continue;

        case '0': case '1': case '2': case '3': case '4':
        case '5': case '6': case '7': case '8': case '9':
        case '+': case '-':
            end = scan_number(s, n, pos, &kind);
            if (end > 0) {
                if (push_tok(p, kind, pos, end, line) < 0) return -1;
                pos = end;
                continue;
            }
            break; /* a lone sign is an operator */

        default:
            if (c >= 'A' && c <= 'Z') {
                end = scan_idchars(s, n, pos + 1);
                if (push_tok(p, K_UPPER, pos, end, line) < 0) return -1;
                pos = end;
                continue;
            }
            if (c >= 'a' && c <= 'z') {
                end = scan_idchars(s, n, pos + 1);
                if (push_tok(p, K_LOWER, pos, end, line) < 0) return -1;
                pos = end;
                continue;
            }
            break;
        }

        /* operators: three-character, then two-character, then single-character */
        kind = -1;
        switch (c) {
        case '<': case '-': case '@': case '[': case '{': case '(': case '=': case '~':
        case '!': case '?': case ':':
            if (pos + 2 < n) {
                for (int k = 0; k < COUNT(OPS3); k++) {
                    const char *t = OPS3[k].text;
                    if (t[0] == s[pos] && t[1] == s[pos + 1] && t[2] == s[pos + 2]) {
                        kind = OPS3[k].kind;
                        end = pos + 3;
                        break;
                    }
                }
            }
            if (kind < 0 && pos + 1 < n) {
                for (int k = 0; k < COUNT(OPS2); k++) {
                    const char *t = OPS2[k].text;
                    if (t[0] == s[pos] && t[1] == s[pos + 1]) {
                        kind = OPS2[k].kind;
                        end = pos + 2;
                        break;
                    }
                }
            }
            break;
        default:
            break;
        }
        if (kind < 0 && ONE_CHAR[c] > 0) {
            kind = ONE_CHAR[c];
            end = pos + 1;
        }
        if (kind < 0) return lex_error(p, pos, line);
        if (push_tok(p, kind, pos, end, line) < 0) return -1;
        pos = end;
    }
    return push_tok(p, K_EOF, pos, pos, line);
}

/* ------------------------------------------------------------------------------------
 * Parser. Each function mirrors the method of the same name in _pyparser.py.
 *
 * Conventions: functions return a new reference, or NULL with an exception set. Locals
 * hold their own references and are released at `done:`. Helpers that take nodes
 * borrow them.
 * ---------------------------------------------------------------------------------- */

/* Mutually recursive guarded functions are declared first; the `_` variants hold the
 * bodies. */
static PyObject *cnf_formula(Parser *p);
static PyObject *tcf_logic(Parser *p);
static PyObject *fof_sequent(Parser *p);
static PyObject *atom_typing(Parser *p, Lang lang);
static PyObject *unit(Parser *p, Lang lang);
static PyObject *logic(Parser *p, Lang lang, int as_term);
static PyObject *primary(Parser *p, Lang lang);
static PyObject *tff_type(Parser *p, int in_parens);
static PyObject *tff_type_primary(Parser *p);
static PyObject *thf_unitary(Parser *p);
static PyObject *source(Parser *p);
static PyObject *general_term(Parser *p);

static PyObject *quantified(Parser *p, Lang lang);
static PyObject *thf_formula(Parser *p);
static PyObject *tff_formula(Parser *p);
static PyObject *fof_formula(Parser *p);
static PyObject *tcf_formula(Parser *p);
static PyObject *thf_type_tail(Parser *p, PyObject *left);
static PyObject *tff_atomic_type(Parser *p);
static PyObject *general_list(Parser *p);
static PyObject *variable_list(Parser *p, Lang lang);

static PyObject *
run_rule(Parser *p, int rule)
{
    switch (rule) {
    case R_THF: return thf_formula(p);
    case R_TFF: return tff_formula(p);
    case R_TCF: return tcf_formula(p);
    case R_CNF: return cnf_formula(p);
    default: return fof_formula(p); /* R_FOF, R_TPI */
    }
}

/* --- words and names ------------------------------------------------------------- */

/* The text of a word token. Quotes come off a single-quoted word when what is left is
 * a valid lower-case word. */
static PyObject *
word_text(Parser *p, const Tok *t)
{
    if (t->kind == K_SQ) {
        const char *a = p->s + t->start + 1;
        Py_ssize_t len = t->end - t->start - 2;
        if (len >= 1 && a[0] >= 'a' && a[0] <= 'z') {
            Py_ssize_t k = 1;
            while (k < len && is_idchar((unsigned char)a[k])) k++;
            if (k == len) return PyUnicode_FromStringAndSize(a, len);
        }
    }
    return tok_text(p, t);
}

static PyObject *
atomic_word(Parser *p)
{
    const Tok *t = CUR(p);
    if (!IS_WORD(t->kind)) return fail_expected(p, "an atomic word");
    p->i++;
    return word_text(p, t);
}

static PyObject *
name(Parser *p)
{
    const Tok *t = CUR(p);
    if (t->kind == K_INTEGER) {
        p->i++;
        return tok_text(p, t);
    }
    if (!IS_WORD(t->kind)) return fail_expected(p, "a name");
    return atomic_word(p);
}

/* --- CNF and TCF ----------------------------------------------------------------- */

static PyObject *fof_atomic(Parser *p, int allow_inequality);

static PyObject *
cnf_literal(Parser *p)
{
    PyObject *atom = NULL, *res = NULL;
    if (!accept(p, O_NOT)) return fof_atomic(p, 1);
    if (accept(p, O_LPAREN)) {
        atom = fof_atomic(p, 0);
        if (atom == NULL) goto done;
        EXPECT(O_RPAREN);
    }
    else {
        atom = fof_atomic(p, 0);
        if (atom == NULL) goto done;
    }
    res = mk_unary(p, O_NOT, atom);
done:
    Py_XDECREF(atom);
    return res;
}

static PyObject *
cnf_formula_(Parser *p)
{
    PyObject *res = NULL, *formula = NULL;
    Vec lits = {0};
    if (accept(p, O_LPAREN)) {
        formula = cnf_formula(p);
        if (formula == NULL) goto done;
        EXPECT(O_RPAREN);
        res = Py_NewRef(formula);
        goto done;
    }
    if (vec_push_new(&lits, cnf_literal(p)) < 0) goto done;
    while (accept(p, O_OR)) {
        if (vec_push_new(&lits, cnf_literal(p)) < 0) goto done;
    }
    if (lits.n == 1) {
        res = Py_NewRef(lits.v[0]);
    }
    else {
        res = mk_nary(p, O_OR, &lits);
    }
done:
    Py_XDECREF(formula);
    vec_free(&lits);
    return res;
}

static PyObject *
cnf_formula(Parser *p)
{
    if (stack_exhausted(p)) return too_deep(p);
    return cnf_formula_(p);
}

static int
at_atom_typing(Parser *p)
{
    Py_ssize_t j = 0;
    while (PEEK(p, j)->kind == O_LPAREN) j++;
    int k = PEEK(p, j)->kind;
    return (IS_WORD(k) || k == K_DISTINCT) && PEEK(p, j + 1)->kind == O_COLON;
}

static PyObject *
tcf_logic_(Parser *p)
{
    PyObject *variables = NULL, *body = NULL, *res = NULL;
    Py_ssize_t mark = p->undo_n;
    if (KIND(p) == O_FORALL && PEEK(p, 1)->kind == O_LBRACK) {
        p->i++;
        variables = variable_list(p, L_TFF);
        if (variables == NULL) goto done;
        body = tcf_logic(p);
        if (body == NULL) goto done;
        res = mk_quantified(p, O_FORALL, variables, body);
        goto done;
    }
    res = cnf_formula(p);
done:
    var_release(p, mark);
    Py_XDECREF(variables);
    Py_XDECREF(body);
    return res;
}

static PyObject *
tcf_logic(Parser *p)
{
    if (stack_exhausted(p)) return too_deep(p);
    return tcf_logic_(p);
}

static PyObject *
tcf_formula(Parser *p)
{
    if (at_atom_typing(p)) return atom_typing(p, L_TFF);
    return tcf_logic(p);
}

/* --- FOF ------------------------------------------------------------------------- */

static PyObject *fof_tuple(Parser *p);

static PyObject *
fof_formula(Parser *p)
{
    Py_ssize_t off = 0;
    while (PEEK(p, off)->kind == O_LPAREN) off++;
    return PEEK(p, off)->kind == O_LBRACK ? fof_sequent(p) : logic(p, L_FOF, 0);
}

static PyObject *
fof_sequent_(Parser *p)
{
    PyObject *left = NULL, *right = NULL, *res = NULL;
    if (accept(p, O_LPAREN)) {
        res = fof_sequent(p);
        if (res == NULL) goto done;
        if (expect(p, O_RPAREN) < 0) {
            Py_CLEAR(res);
        }
        goto done;
    }
    left = fof_tuple(p);
    if (left == NULL) goto done;
    EXPECT(O_SEQUENT);
    right = fof_tuple(p);
    if (right == NULL) goto done;
    res = mk_binary(p, O_SEQUENT, left, right);
done:
    Py_XDECREF(left);
    Py_XDECREF(right);
    return res;
}

static PyObject *
fof_sequent(Parser *p)
{
    if (stack_exhausted(p)) return too_deep(p);
    return fof_sequent_(p);
}

static PyObject *
fof_tuple(Parser *p)
{
    PyObject *res = NULL;
    Vec items = {0};
    EXPECT(O_LBRACK);
    if (KIND(p) != O_RBRACK) {
        if (vec_push_new(&items, logic(p, L_FOF, 0)) < 0) goto done;
        while (accept(p, O_COMMA)) {
            if (vec_push_new(&items, logic(p, L_FOF, 0)) < 0) goto done;
        }
    }
    EXPECT(O_RBRACK);
    res = mk_tuple_node(p, &items);
done:
    vec_free(&items);
    return res;
}

static PyObject *
fof_atomic(Parser *p, int allow_inequality)
{
    PyObject *left = NULL, *right = NULL, *res = NULL;
    const Tok *start = CUR(p);
    left = primary(p, L_FOF);
    if (left == NULL) goto done;
    int op = KIND(p);
    if (op == O_EQ || (op == O_NEQ && allow_inequality)) {
        p->i++;
        right = primary(p, L_FOF);
        if (right == NULL) goto done;
        res = mk_binary(p, op, left, right);
        goto done;
    }
    if (!ISA(left, S(p)->c_Function)) {
        fail_term(p, start);
        goto done;
    }
    res = Py_NewRef(left);
done:
    Py_XDECREF(left);
    Py_XDECREF(right);
    return res;
}

/* --- shared FOF/TFF/THF logic formulae --------------------------------------------- */

static inline int
is_nonassoc(int k)
{
    return k == O_IFF || k == O_IMPLIES || k == O_IMPLIED_BY || k == O_XOR || k == O_NOR ||
           k == O_NAND;
}

static inline int
is_chain(Lang lang, int k)
{
    return k == O_OR || k == O_AND || (lang == L_THF && k == O_AT);
}

static inline int
is_unary_op(Lang lang, int k)
{
    if (k == O_NOT) return 1;
    if (lang == L_FOF) return 0;
    return k == O_BOX || k == O_DIAMOND || k == O_BRACE_DOT || k == O_PAREN_DOT;
}

static inline int
is_quantifier_op(Lang lang, int k)
{
    switch (k) {
    case O_FORALL: case O_EXISTS: return 1;
    case O_HASH: return lang != L_FOF;
    case O_CARET: case O_CHOICE: case O_DESCRIPTION: case O_TYPE_FORALL:
    case O_TYPE_EXISTS: return lang == L_THF;
    default: return 0;
    }
}

static inline int
is_thf_connective_term(int k)
{
    return is_nonassoc(k) || k == O_BOX || k == O_DIAMOND || k == O_BRACE_DOT ||
           k == O_PAREN_DOT || k == O_OR || k == O_AND || k == O_EQ || k == O_NEQ ||
           k == O_NOT;
}

static inline int
is_th1_term(int k)
{
    return k == O_BANGBANG || k == O_QQ || k == O_ATAT_PLUS || k == O_ATAT_MINUS ||
           k == O_AT_EQ;
}

/* Numbers, distinct objects and tuples can't stand alone as TFF formulae. */
static int
require_formula(Parser *p, Lang lang, PyObject *node, const Tok *start)
{
    State *st = S(p);
    if (lang == L_TFF && (ISA(node, st->c_Number) || ISA(node, st->c_DistinctObject) ||
                          ISA(node, st->c_Tuple))) {
        fail_term(p, start);
        return -1;
    }
    return 0;
}

static PyObject *
definition(Parser *p, Lang lang, PyObject *left, const Tok *start)
{
    State *st = S(p);
    PyObject *right = NULL, *res = NULL;
    int ok;
    if (lang == L_THF) {
        ok = ISA(left, st->c_Function) || ISA(left, st->c_Tuple) ||
             ISA(left, st->c_ConnectiveTerm) || ISA(left, st->c_LongConnective) ||
             ISA(left, st->c_Let) || ISA(left, st->c_Number) ||
             ISA(left, st->c_DistinctObject);
    }
    else {
        ok = ISA(left, st->c_Function) || ISA(left, st->c_ModalAtom) || ISA(left, st->c_Let);
    }
    if (!ok) return fail_at(p, start, "the left side of '==' must be atomic");
    EXPECT(O_DEFEQ);
    right = logic(p, lang, 1);
    if (right == NULL) goto done;
    res = mk_binary(p, O_DEFEQ, left, right);
done:
    Py_XDECREF(right);
    return res;
}

static PyObject *
sequent(Parser *p, Lang lang, PyObject *left, const Tok *start)
{
    PyObject *right = NULL, *res = NULL;
    if (!ISA(left, S(p)->c_Tuple)) {
        return fail_at(p, start, "the left side of '-->' must be a tuple");
    }
    EXPECT(O_SEQUENT);
    if (KIND(p) != O_LBRACK) {
        fail_expected(p, "a tuple after '-->'");
        goto done;
    }
    right = primary(p, lang);
    if (right == NULL) goto done;
    res = mk_binary(p, O_SEQUENT, left, right);
done:
    Py_XDECREF(right);
    return res;
}

static PyObject *
logic_(Parser *p, Lang lang, int as_term)
{
    State *st = S(p);
    PyObject *left = NULL, *right = NULL, *res = NULL;
    Vec args = {0};
    const Tok *start = CUR(p);
    int op;

    left = unit(p, lang);
    if (left == NULL) goto done;
    op = KIND(p);

    if (is_nonassoc(op)) {
        if (require_formula(p, lang, left, start) < 0) goto done;
        p->i++;
        const Tok *rs = CUR(p);
        right = unit(p, lang);
        if (right == NULL) goto done;
        if (require_formula(p, lang, right, rs) < 0) goto done;
        res = mk_binary(p, op, left, right);
        goto done;
    }
    if (is_chain(lang, op)) {
        if (require_formula(p, lang, left, start) < 0) goto done;
        if (vec_push_new(&args, Py_NewRef(left)) < 0) goto done;
        while (KIND(p) == op) {
            p->i++;
            const Tok *rs = CUR(p);
            PyObject *r = unit(p, lang);
            if (r == NULL) goto done;
            if (require_formula(p, lang, r, rs) < 0) {
                Py_DECREF(r);
                goto done;
            }
            if (vec_push_new(&args, r) < 0) goto done;
        }
        res = mk_nary(p, op, &args);
        goto done;
    }
    if (lang != L_FOF) {
        if (op == O_DEFEQ) {
            res = definition(p, lang, left, start);
            goto done;
        }
        if (op == O_SEQUENT) {
            res = sequent(p, lang, left, start);
            goto done;
        }
    }
    if (lang == L_THF && (op == O_GT || op == O_STAR || op == O_PLUS)) {
        if (start->kind != O_LPAREN &&
            (ISA(left, st->c_Unary) || ISA(left, st->c_Equals) || ISA(left, st->c_NotEquals))) {
            fail_at(p, start, "'%s' needs a unitary left operand; add parentheses",
                    OP_TEXT[op]);
            goto done;
        }
        res = thf_type_tail(p, left);
        goto done;
    }
    if (!as_term) {
        if (require_formula(p, lang, left, start) < 0) goto done;
    }
    res = Py_NewRef(left);
done:
    Py_XDECREF(left);
    Py_XDECREF(right);
    vec_free(&args);
    return res;
}

static PyObject *
logic(Parser *p, Lang lang, int as_term)
{
    if (stack_exhausted(p)) return too_deep(p);
    return logic_(p, lang, as_term);
}

static PyObject *
variable(Parser *p, Lang lang)
{
    State *st = S(p);
    PyObject *ty = NULL, *res = NULL;
    const Tok *t = CUR(p);
    if (t->kind != K_UPPER) return fail_expected(p, "a variable");
    p->i++;
    Py_ssize_t index = var_bind(p, t);
    if (index < 0) return NULL;
    if (lang == L_FOF) {
        res = mk_variable(p, t, index, NULL);
    }
    else if (lang == L_THF) {
        EXPECT(O_COLON);
        p->type_depth++;
        ty = thf_unitary(p);
        if (ty != NULL &&
            (KIND(p) == O_GT || KIND(p) == O_STAR || KIND(p) == O_PLUS || KIND(p) == O_AT)) {
            PyObject *full = thf_type_tail(p, ty);
            Py_SETREF(ty, full);
        }
        p->type_depth--;
        if (ty == NULL) goto done;
        res = mk_variable(p, t, index, ty);
    }
    else if (accept(p, O_COLON)) {
        ty = tff_atomic_type(p);
        if (ty == NULL) goto done;
        res = mk_variable(p, t, index, ty);
    }
    else {
        res = mk_variable(p, t, index, NULL);
    }
done:
    (void)st;
    Py_XDECREF(ty);
    return res;
}

static PyObject *
variable_list(Parser *p, Lang lang)
{
    PyObject *res = NULL;
    Vec vars = {0};
    EXPECT(O_LBRACK);
    if (vec_push_new(&vars, variable(p, lang)) < 0) goto done;
    while (accept(p, O_COMMA)) {
        if (vec_push_new(&vars, variable(p, lang)) < 0) goto done;
    }
    EXPECT(O_RBRACK);
    EXPECT(O_COLON);
    res = vec_tuple(&vars);
done:
    vec_free(&vars);
    return res;
}

static PyObject *
quantified(Parser *p, Lang lang)
{
    PyObject *variables = NULL, *body = NULL, *res = NULL;
    int op = KIND(p);
    Py_ssize_t mark = p->undo_n;
    p->i++;
    variables = variable_list(p, lang);
    if (variables == NULL) goto done;
    const Tok *body_start = CUR(p);
    body = unit(p, lang);
    if (body == NULL) goto done;
    if (require_formula(p, lang, body, body_start) < 0) goto done;
    res = mk_quantified(p, op, variables, body);
done:
    var_release(p, mark);
    Py_XDECREF(variables);
    Py_XDECREF(body);
    return res;
}

static PyObject *
unit_(Parser *p, Lang lang)
{
    State *st = S(p);
    PyObject *arg = NULL, *left = NULL, *right = NULL, *res = NULL;
    const Tok *tok = CUR(p);
    int kind = tok->kind;

    if (is_unary_op(lang, kind)) {
        p->i++;
        const Tok *arg_start = CUR(p);
        arg = unit(p, lang);
        if (arg == NULL) goto done;
        if (require_formula(p, lang, arg, arg_start) < 0) goto done;
        res = mk_unary(p, kind, arg);
        goto done;
    }
    if (is_quantifier_op(lang, kind) && PEEK(p, 1)->kind == O_LBRACK) {
        res = quantified(p, lang);
        goto done;
    }
    if (lang == L_FOF) {
        if (accept(p, O_LPAREN)) {
            res = logic(p, L_FOF, 0);
            if (res == NULL) goto done;
            if (expect(p, O_RPAREN) < 0) Py_CLEAR(res);
            goto done;
        }
        res = fof_atomic(p, 1);
        goto done;
    }
    left = primary(p, lang);
    if (left == NULL) goto done;
    {
        int op = KIND(p);
        if (op == O_EQ || op == O_NEQ) {
            p->i++;
            right = primary(p, lang);
            if (right == NULL) goto done;
            res = mk_binary(p, op, left, right);
            goto done;
        }
    }
    res = Py_NewRef(left);
done:
    (void)st;
    Py_XDECREF(arg);
    Py_XDECREF(left);
    Py_XDECREF(right);
    return res;
}

static PyObject *
unit(Parser *p, Lang lang)
{
    if (stack_exhausted(p)) return too_deep(p);
    return unit_(p, lang);
}

/* --- terms -------------------------------------------------------------------- */

static PyObject *
argument(Parser *p, Lang lang)
{
    if (lang == L_FOF) return primary(p, L_FOF);
    return logic(p, lang, 1);
}

static PyObject *
p_tuple(Parser *p, Lang lang)
{
    PyObject *res = NULL;
    Vec items = {0};
    EXPECT(O_LBRACK);
    if (KIND(p) != O_RBRACK) {
        if (vec_push_new(&items, argument(p, lang)) < 0) goto done;
        while (accept(p, O_COMMA)) {
            if (vec_push_new(&items, argument(p, lang)) < 0) goto done;
        }
    }
    EXPECT(O_RBRACK);
    res = mk_tuple_node(p, &items);
done:
    vec_free(&items);
    return res;
}

static PyObject *
connective_parameter(Parser *p, Lang lang)
{
    State *st = S(p);
    PyObject *term = NULL, *param = NULL, *res = NULL;
    if (accept(p, O_HASH)) {
        term = primary(p, L_TFF);
        if (term == NULL) goto done;
        res = mk(st->c_Index, 1, st->n_term, term);
        goto done;
    }
    const Tok *start = CUR(p);
    param = logic(p, lang, 1);
    if (param == NULL) goto done;
    if (!ISA(param, st->c_Definition)) {
        fail_at(p, start, "expected '#index' or 'key == value'");
        goto done;
    }
    res = Py_NewRef(param);
done:
    Py_XDECREF(term);
    Py_XDECREF(param);
    return res;
}

static PyObject *
long_connective(Parser *p, Lang lang)
{
    State *st = S(p);
    PyObject *cname = NULL, *params_t = NULL, *connective = NULL, *args_t = NULL, *res = NULL;
    Vec params = {0}, args = {0};
    EXPECT(O_LBRACE);
    const Tok *t = CUR(p);
    if (t->kind != K_DEFINED && t->kind != K_SYSTEM) {
        fail_expected(p, "a connective name");
        goto done;
    }
    p->i++;
    if (accept(p, O_LPAREN)) {
        if (vec_push_new(&params, connective_parameter(p, lang)) < 0) goto done;
        while (accept(p, O_COMMA)) {
            if (vec_push_new(&params, connective_parameter(p, lang)) < 0) goto done;
        }
        EXPECT(O_RPAREN);
    }
    EXPECT(O_RBRACE);
    cname = tok_text(p, t);
    if (cname == NULL) goto done;
    params_t = vec_tuple(&params);
    if (params_t == NULL) goto done;
    connective = mk(st->c_LongConnective, 2, st->n_name, cname, st->n_params, params_t);
    if (connective == NULL) goto done;
    if (lang != L_TFF) {
        res = Py_NewRef(connective);
        goto done;
    }
    EXPECT(O_AT);
    EXPECT(O_LPAREN);
    if (vec_push_new(&args, argument(p, L_TFF)) < 0) goto done;
    while (accept(p, O_COMMA)) {
        if (vec_push_new(&args, argument(p, L_TFF)) < 0) goto done;
    }
    EXPECT(O_RPAREN);
    args_t = vec_tuple(&args);
    if (args_t == NULL) goto done;
    res = mk(st->c_ModalAtom, 2, st->n_connective, connective, st->n_args, args_t);
done:
    Py_XDECREF(cname);
    Py_XDECREF(params_t);
    Py_XDECREF(connective);
    Py_XDECREF(args_t);
    vec_free(&params);
    vec_free(&args);
    return res;
}

/* Tell the tuple in `[a,b] := t` from the definition list `[a := t, ...]`. */
static int
bracket_is_let_lhs(Parser *p)
{
    int depth = 0;
    for (Py_ssize_t j = p->i; j < p->ntok; j++) {
        int k = p->toks[j].kind;
        if (k == O_LBRACK) {
            depth++;
        }
        else if (k == O_RBRACK) {
            depth--;
            if (depth == 0) {
                Py_ssize_t m = j + 1 < p->ntok ? j + 1 : p->ntok - 1;
                return p->toks[m].kind == O_ASSIGN;
            }
        }
    }
    return 0;
}

static PyObject *
let_definition(Parser *p, Lang lang)
{
    State *st = S(p);
    PyObject *left = NULL, *right = NULL, *res = NULL;
    const Tok *start = CUR(p);
    if (lang == L_THF) {
        left = logic(p, L_THF, 0);
        if (left == NULL) goto done;
    }
    else {
        left = primary(p, L_TFF);
        if (left == NULL) goto done;
        if (!(ISA(left, st->c_Function) || ISA(left, st->c_Tuple))) {
            fail_at(p, start, "the left side of ':=' must be an atom or tuple");
            goto done;
        }
    }
    EXPECT(O_ASSIGN);
    right = logic(p, lang, lang == L_TFF);
    if (right == NULL) goto done;
    res = mk_binary(p, O_ASSIGN, left, right);
done:
    Py_XDECREF(left);
    Py_XDECREF(right);
    return res;
}

static PyObject *
let(Parser *p, Lang lang)
{
    State *st = S(p);
    PyObject *types_t = NULL, *defs_t = NULL, *body = NULL, *res = NULL;
    Vec types = {0}, defs = {0};
    p->i++; /* $let */
    EXPECT(O_LPAREN);
    if (accept(p, O_LBRACK)) {
        if (vec_push_new(&types, atom_typing(p, lang)) < 0) goto done;
        while (accept(p, O_COMMA)) {
            if (vec_push_new(&types, atom_typing(p, lang)) < 0) goto done;
        }
        EXPECT(O_RBRACK);
    }
    else if (vec_push_new(&types, atom_typing(p, lang)) < 0) {
        goto done;
    }
    EXPECT(O_COMMA);
    if (KIND(p) == O_LBRACK && !bracket_is_let_lhs(p)) {
        p->i++;
        if (vec_push_new(&defs, let_definition(p, lang)) < 0) goto done;
        while (accept(p, O_COMMA)) {
            if (vec_push_new(&defs, let_definition(p, lang)) < 0) goto done;
        }
        EXPECT(O_RBRACK);
    }
    else if (vec_push_new(&defs, let_definition(p, lang)) < 0) {
        goto done;
    }
    EXPECT(O_COMMA);
    body = logic(p, lang, lang == L_TFF);
    if (body == NULL) goto done;
    EXPECT(O_RPAREN);
    types_t = vec_tuple(&types);
    defs_t = vec_tuple(&defs);
    if (types_t == NULL || defs_t == NULL) goto done;
    res = mk(st->c_Let, 3, st->n_types, types_t, st->n_definitions, defs_t, st->n_body, body);
done:
    Py_XDECREF(types_t);
    Py_XDECREF(defs_t);
    Py_XDECREF(body);
    vec_free(&types);
    vec_free(&defs);
    return res;
}

static PyObject *
primary_(Parser *p, Lang lang)
{
    State *st = S(p);
    const Tok *tok = CUR(p);
    int kind = tok->kind;
    PyObject *name_s = NULL, *args_t = NULL, *res = NULL;
    Vec args = {0};

    if (kind == K_UPPER) {
        p->i++;
        return mk_variable_use(p, tok);
    }
    if (IS_NUM(kind)) {
        p->i++;
        return mk_word_node(p, st->c_Number, st->n_text, tok);
    }
    if (kind == K_DISTINCT) {
        p->i++;
        return mk_word_node(p, st->c_DistinctObject, st->n_text, tok);
    }
    if (IS_WORD(kind) || kind == K_DEFINED || kind == K_SYSTEM) {
        if (kind == K_DEFINED && lang != L_FOF && TEXT_IS(p, tok, "$let") &&
            PEEK(p, 1)->kind == O_LPAREN) {
            return let(p, lang);
        }
        p->i++;
        name_s = word_text(p, tok);
        if (name_s == NULL) goto done;
        /* Reserve this use's place in the order of first appearance now, and fill in the
         * arity once the arguments are read. $-symbols are not recorded. */
        Py_ssize_t slot = -1;
        if (p->numbering && !p->type_depth && IS_WORD(kind)) {
            slot = ev_reserve(p);
            if (slot < 0) goto done;
        }
        if (accept(p, O_LPAREN)) {
            if (vec_push_new(&args, argument(p, lang)) < 0) goto done;
            while (accept(p, O_COMMA)) {
                if (vec_push_new(&args, argument(p, lang)) < 0) goto done;
            }
            EXPECT(O_RPAREN);
            if (slot >= 0) ev_fill(p, slot, name_s, args.n);
            args_t = vec_tuple(&args);
            if (args_t == NULL) goto done;
            res = mk_function(p, name_s, args_t);
            goto done;
        }
        if (slot >= 0) ev_fill(p, slot, name_s, 0);
        res = mk_function(p, name_s, NULL);
        goto done;
    }
    if (lang != L_FOF) {
        if (kind == O_LPAREN) {
            const Tok *a = PEEK(p, 1);
            if (lang == L_THF && is_thf_connective_term(a->kind) &&
                PEEK(p, 2)->kind == O_RPAREN) {
                int op = a->kind;
                p->i += 3;
                return mk(st->c_ConnectiveTerm, 1, st->n_op, st->opstr[op]);
            }
            p->i++;
            res = logic(p, lang, 0);
            if (res == NULL) goto done;
            if (expect(p, O_RPAREN) < 0) Py_CLEAR(res);
            goto done;
        }
        if (kind == O_LBRACK) return p_tuple(p, lang);
        if (kind == O_LBRACE) return long_connective(p, lang);
        if (lang == L_THF && is_th1_term(kind)) {
            p->i++;
            return mk(st->c_ConnectiveTerm, 1, st->n_op, st->opstr[kind]);
        }
    }
    fail_expected(p, "a term");
done:
    Py_XDECREF(name_s);
    Py_XDECREF(args_t);
    vec_free(&args);
    return res;
}

static PyObject *
primary(Parser *p, Lang lang)
{
    if (stack_exhausted(p)) return too_deep(p);
    return primary_(p, lang);
}

/* --- type declarations ----------------------------------------------------------- */

static PyObject *
atom_typing_(Parser *p, Lang lang)
{
    State *st = S(p);
    PyObject *typing = NULL, *name_s = NULL, *ty = NULL, *res = NULL;
    if (accept(p, O_LPAREN)) {
        typing = atom_typing(p, lang);
        if (typing == NULL) goto done;
        EXPECT(O_RPAREN);
        res = Py_NewRef(typing);
        goto done;
    }
    const Tok *t = CUR(p);
    if (!IS_WORD(t->kind) && t->kind != K_DISTINCT) {
        fail_expected(p, "an atom to declare a type for");
        goto done;
    }
    p->i++;
    EXPECT(O_COLON);
    if (lang == L_THF) {
        p->type_depth++;
        ty = thf_unitary(p);
        if (ty != NULL && (KIND(p) == O_GT || KIND(p) == O_STAR || KIND(p) == O_PLUS ||
                           KIND(p) == O_AT)) {
            PyObject *full = thf_type_tail(p, ty);
            Py_SETREF(ty, full);
        }
        p->type_depth--;
    }
    else {
        ty = tff_type(p, 0);
    }
    if (ty == NULL) goto done;
    name_s = word_text(p, t);
    if (name_s == NULL) goto done;
    res = mk(st->c_Typing, 2, st->n_atom, name_s, st->n_type, ty);
done:
    Py_XDECREF(typing);
    Py_XDECREF(name_s);
    Py_XDECREF(ty);
    return res;
}

static PyObject *
atom_typing(Parser *p, Lang lang)
{
    if (stack_exhausted(p)) return too_deep(p);
    return atom_typing_(p, lang);
}

static int
at_subtype(Parser *p)
{
    int k = KIND(p);
    return (IS_WORD(k) || k == K_DEFINED || k == K_SYSTEM || k == K_DISTINCT) &&
           PEEK(p, 1)->kind == O_SUBTYPE;
}

static PyObject *
subtype_atom(Parser *p)
{
    State *st = S(p);
    PyObject *w = NULL, *res = NULL;
    const Tok *t = CUR(p);
    if (t->kind == K_DISTINCT) {
        p->i++;
        return mk_word_node(p, st->c_DistinctObject, st->n_text, t);
    }
    if (IS_WORD(t->kind) || t->kind == K_DEFINED || t->kind == K_SYSTEM) {
        p->i++;
        w = word_text(p, t);
        res = mk_function(p, w, NULL);
        Py_XDECREF(w);
        return res;
    }
    return fail_expected(p, "a type name");
}

static PyObject *
subtype(Parser *p)
{
    PyObject *left = NULL, *right = NULL, *res = NULL;
    left = subtype_atom(p);
    if (left == NULL) goto done;
    EXPECT(O_SUBTYPE);
    right = subtype_atom(p);
    if (right == NULL) goto done;
    res = mk_binary(p, O_SUBTYPE, left, right);
done:
    Py_XDECREF(left);
    Py_XDECREF(right);
    return res;
}

static PyObject *
tff_formula(Parser *p)
{
    if (at_atom_typing(p)) return atom_typing(p, L_TFF);
    if (at_subtype(p)) return subtype(p);
    return logic(p, L_TFF, 0);
}

static PyObject *
thf_formula(Parser *p)
{
    if (at_atom_typing(p)) return atom_typing(p, L_THF);
    if (at_subtype(p)) return subtype(p);
    return logic(p, L_THF, 0);
}

static inline int
is_type_quantifier(int k)
{
    return k == O_TYPE_FORALL || k == O_TYPE_EXISTS;
}

static PyObject *
tff_type_(Parser *p, int in_parens)
{
    State *st = S(p);
    PyObject *variables = NULL, *body = NULL, *left = NULL, *right = NULL, *res = NULL;
    Vec args = {0};
    Py_ssize_t mark = p->undo_n;

    if (is_type_quantifier(KIND(p))) {
        int op = KIND(p);
        p->i++;
        variables = variable_list(p, L_TFF);
        if (variables == NULL) goto done;
        const Tok *body_start = CUR(p);
        body = is_type_quantifier(KIND(p)) ? tff_type(p, 0) : tff_type_primary(p);
        if (body == NULL) goto done;
        if (ISA(body, st->c_NAry)) {
            fail_at(p, body_start, "a product type must be followed by '>'");
            goto done;
        }
        res = mk_quantified(p, op, variables, body);
        goto done;
    }
    const Tok *start = CUR(p);
    left = tff_type_primary(p);
    if (left == NULL) goto done;
    if ((ISA(left, st->c_Binary) || ISA(left, st->c_Quantified)) &&
        (KIND(p) == O_GT || KIND(p) == O_STAR)) {
        fail_at(p, start, "this type can't be an operand of '%s'", OP_TEXT[KIND(p)]);
        goto done;
    }
    if (accept(p, O_GT)) {
        right = tff_atomic_type(p);
        if (right == NULL) goto done;
        res = mk_binary(p, O_GT, left, right);
        goto done;
    }
    if (in_parens && KIND(p) == O_STAR) {
        if (vec_push_new(&args, Py_NewRef(left)) < 0) goto done;
        while (accept(p, O_STAR)) {
            if (vec_push_new(&args, tff_atomic_type(p)) < 0) goto done;
        }
        res = mk_nary(p, O_STAR, &args);
        goto done;
    }
    if (ISA(left, st->c_NAry)) {
        fail_at(p, start, "a product type must be followed by '>'");
        goto done;
    }
    res = Py_NewRef(left);
done:
    var_release(p, mark);
    Py_XDECREF(variables);
    Py_XDECREF(body);
    Py_XDECREF(left);
    Py_XDECREF(right);
    vec_free(&args);
    return res;
}

static PyObject *
tff_type(Parser *p, int in_parens)
{
    if (stack_exhausted(p)) return too_deep(p);
    return tff_type_(p, in_parens);
}

static PyObject *
tff_type_primary_(Parser *p)
{
    PyObject *inner = NULL, *name_s = NULL, *args_t = NULL, *res = NULL;
    Vec items = {0};
    const Tok *tok = CUR(p);

    if (accept(p, O_LPAREN)) {
        inner = tff_type(p, 1);
        if (inner == NULL) goto done;
        EXPECT(O_RPAREN);
        res = Py_NewRef(inner);
        goto done;
    }
    if (tok->kind == O_LBRACK) {
        p->i++;
        if (vec_push_new(&items, tff_type(p, 0)) < 0) goto done;
        while (accept(p, O_COMMA)) {
            if (vec_push_new(&items, tff_type(p, 0)) < 0) goto done;
        }
        EXPECT(O_RBRACK);
        res = mk_tuple_node(p, &items);
        goto done;
    }
    if (tok->kind == K_UPPER) {
        p->i++;
        return mk_variable_use(p, tok);
    }
    if (tok->kind == K_DEFINED) {
        p->i++;
        name_s = tok_text(p, tok);
        res = mk_function(p, name_s, NULL);
        goto done;
    }
    if (IS_WORD(tok->kind)) {
        p->i++;
        name_s = word_text(p, tok);
        if (name_s == NULL) goto done;
        if (accept(p, O_LPAREN)) {
            if (vec_push_new(&items, tff_atomic_type(p)) < 0) goto done;
            while (accept(p, O_COMMA)) {
                if (vec_push_new(&items, tff_atomic_type(p)) < 0) goto done;
            }
            EXPECT(O_RPAREN);
            args_t = vec_tuple(&items);
            if (args_t == NULL) goto done;
            res = mk_function(p, name_s, args_t);
            goto done;
        }
        res = mk_function(p, name_s, NULL);
        goto done;
    }
    fail_expected(p, "a type");
done:
    Py_XDECREF(inner);
    Py_XDECREF(name_s);
    Py_XDECREF(args_t);
    vec_free(&items);
    return res;
}

static PyObject *
tff_type_primary(Parser *p)
{
    if (stack_exhausted(p)) return too_deep(p);
    return tff_type_primary_(p);
}

static PyObject *
tff_atomic_type(Parser *p)
{
    State *st = S(p);
    const Tok *start = CUR(p);
    PyObject *node = tff_type_primary(p);
    if (node == NULL) return NULL;
    if (!(ISA(node, st->c_Variable) || ISA(node, st->c_Function) || ISA(node, st->c_Tuple))) {
        Py_DECREF(node);
        return fail_at(p, start, "expected an atomic type");
    }
    return node;
}

static PyObject *
thf_unitary_(Parser *p)
{
    if (is_quantifier_op(L_THF, KIND(p)) && PEEK(p, 1)->kind == O_LBRACK) {
        return quantified(p, L_THF);
    }
    return primary(p, L_THF);
}

static PyObject *
thf_unitary(Parser *p)
{
    if (stack_exhausted(p)) return too_deep(p);
    return thf_unitary_(p);
}

static PyObject *
thf_type_tail(Parser *p, PyObject *left)
{
    PyObject *result = NULL, *tmp = NULL;
    Vec items = {0};
    int op = KIND(p);
    if (op == O_GT) {
        if (vec_push_new(&items, Py_NewRef(left)) < 0) goto done;
        while (accept(p, O_GT)) {
            if (vec_push_new(&items, thf_unitary(p)) < 0) goto done;
        }
        result = Py_NewRef(items.v[items.n - 1]);
        for (Py_ssize_t k = items.n - 2; k >= 0; k--) {
            tmp = mk_binary(p, O_GT, items.v[k], result);
            Py_SETREF(result, tmp);
            if (result == NULL) goto done;
        }
        goto done;
    }
    if (vec_push_new(&items, Py_NewRef(left)) < 0) goto done;
    while (accept(p, op)) {
        PyObject *r = op == O_AT ? unit(p, L_THF) : thf_unitary(p);
        if (vec_push_new(&items, r) < 0) goto done;
    }
    result = mk_nary(p, op, &items);
done:
    vec_free(&items);
    return result;
}

/* --- annotations and non-logical data -------------------------------------------- */

static PyObject *
general_data(Parser *p)
{
    State *st = S(p);
    PyObject *name_s = NULL, *args_t = NULL, *formula = NULL, *lang_s = NULL, *res = NULL;
    Vec args = {0};
    const Tok *tok = CUR(p);
    int kind = tok->kind;

    if (kind == K_UPPER) {
        p->i++;
        return mk_variable_use(p, tok);
    }
    if (IS_NUM(kind)) {
        p->i++;
        return mk_word_node(p, st->c_Number, st->n_text, tok);
    }
    if (kind == K_DISTINCT) {
        p->i++;
        return mk_word_node(p, st->c_DistinctObject, st->n_text, tok);
    }
    if (IS_WORD(kind)) {
        p->i++;
        name_s = word_text(p, tok);
        if (name_s == NULL) goto done;
        if (accept(p, O_LPAREN)) {
            if (vec_push_new(&args, general_term(p)) < 0) goto done;
            while (accept(p, O_COMMA)) {
                if (vec_push_new(&args, general_term(p)) < 0) goto done;
            }
            EXPECT(O_RPAREN);
            args_t = vec_tuple(&args);
            if (args_t == NULL) goto done;
            res = mk_function(p, name_s, args_t);
            goto done;
        }
        res = mk_function(p, name_s, NULL);
        goto done;
    }
    if (kind == K_DEFINED && PEEK(p, 1)->kind == O_LPAREN) {
        int rule = -1;
        int is_fot = 0;
        if (TEXT_IS(p, tok, "$thf")) rule = R_THF;
        else if (TEXT_IS(p, tok, "$tff")) rule = R_TFF;
        else if (TEXT_IS(p, tok, "$fof")) rule = R_FOF;
        else if (TEXT_IS(p, tok, "$cnf")) rule = R_CNF;
        else if (TEXT_IS(p, tok, "$fot")) is_fot = 1;
        if (rule >= 0 || is_fot) {
            p->i += 2;
            formula = is_fot ? primary(p, L_FOF) : run_rule(p, rule);
            if (formula == NULL) goto done;
            EXPECT(O_RPAREN);
            lang_s = PyUnicode_FromStringAndSize(p->s + tok->start + 1, tok->end - tok->start - 1);
            if (lang_s == NULL) goto done;
            res = mk(st->c_FormulaData, 2, st->n_language, lang_s, st->n_formula, formula);
            goto done;
        }
    }
    fail_expected(p, "general data");
done:
    Py_XDECREF(name_s);
    Py_XDECREF(args_t);
    Py_XDECREF(formula);
    Py_XDECREF(lang_s);
    vec_free(&args);
    return res;
}

static PyObject *
general_list(Parser *p)
{
    PyObject *res = NULL;
    Vec items = {0};
    EXPECT(O_LBRACK);
    if (KIND(p) != O_RBRACK) {
        if (vec_push_new(&items, general_term(p)) < 0) goto done;
        while (accept(p, O_COMMA)) {
            if (vec_push_new(&items, general_term(p)) < 0) goto done;
        }
    }
    EXPECT(O_RBRACK);
    res = mk_tuple_node(p, &items);
done:
    vec_free(&items);
    return res;
}

static PyObject *
general_term_(Parser *p)
{
    PyObject *data = NULL, *rest = NULL, *res = NULL;
    if (KIND(p) == O_LBRACK) return general_list(p);
    data = general_data(p);
    if (data == NULL) goto done;
    if (accept(p, O_COLON)) {
        rest = general_term(p);
        if (rest == NULL) goto done;
        res = mk_binary(p, O_COLON, data, rest);
        goto done;
    }
    res = Py_NewRef(data);
done:
    Py_XDECREF(data);
    Py_XDECREF(rest);
    return res;
}

static PyObject *
general_term(Parser *p)
{
    if (stack_exhausted(p)) return too_deep(p);
    return general_term_(p);
}

static PyObject *
parent(Parser *p)
{
    State *st = S(p);
    PyObject *src = NULL, *details = NULL, *res = NULL;
    src = source(p);
    if (src == NULL) goto done;
    if (accept(p, O_COLON)) {
        details = general_term(p);
        if (details == NULL) goto done;
    }
    else {
        details = Py_NewRef(Py_None);
    }
    res = mk(st->c_Parent, 2, st->n_source, src, st->n_details, details);
done:
    Py_XDECREF(src);
    Py_XDECREF(details);
    return res;
}

static PyObject *
derivation_source(Parser *p, PyObject *cls, PyObject *kind_field)
{
    State *st = S(p);
    PyObject *word = NULL, *info = NULL, *parents_t = NULL, *res = NULL;
    Vec parents = {0};
    p->i += 2;
    word = atomic_word(p);
    if (word == NULL) goto done;
    EXPECT(O_COMMA);
    info = general_list(p);
    if (info == NULL) goto done;
    EXPECT(O_COMMA);
    EXPECT(O_LBRACK);
    if (KIND(p) != O_RBRACK) {
        if (vec_push_new(&parents, parent(p)) < 0) goto done;
        while (accept(p, O_COMMA)) {
            if (vec_push_new(&parents, parent(p)) < 0) goto done;
        }
    }
    EXPECT(O_RBRACK);
    EXPECT(O_RPAREN);
    parents_t = vec_tuple(&parents);
    if (parents_t == NULL) goto done;
    res = mk(cls, 3, kind_field, word, st->n_info, info, st->n_parents, parents_t);
done:
    Py_XDECREF(word);
    Py_XDECREF(info);
    Py_XDECREF(parents_t);
    vec_free(&parents);
    return res;
}

static PyObject *
source_(Parser *p)
{
    State *st = S(p);
    PyObject *file = NULL, *nm = NULL, *sources_t = NULL, *res = NULL;
    Vec sources = {0};
    const Tok *tok = CUR(p);

    if (tok->kind == O_LBRACK) {
        p->i++;
        if (vec_push_new(&sources, source(p)) < 0) goto done;
        while (accept(p, O_COMMA)) {
            if (vec_push_new(&sources, source(p)) < 0) goto done;
        }
        EXPECT(O_RBRACK);
        sources_t = vec_tuple(&sources);
        if (sources_t == NULL) goto done;
        res = mk(st->c_SourceList, 1, st->n_sources, sources_t);
        goto done;
    }
    if (PEEK(p, 1)->kind == O_LPAREN && tok->kind == K_LOWER) {
        if (TEXT_IS(p, tok, "inference")) {
            return derivation_source(p, st->c_Inference, st->n_rule);
        }
        if (TEXT_IS(p, tok, "introduced")) {
            return derivation_source(p, st->c_Introduced, st->n_kind);
        }
        if (TEXT_IS(p, tok, "file")) {
            p->i += 2;
            file = atomic_word(p);
            if (file == NULL) goto done;
            if (accept(p, O_COMMA)) {
                nm = name(p);
                if (nm == NULL) goto done;
            }
            else {
                nm = Py_NewRef(Py_None);
            }
            EXPECT(O_RPAREN);
            res = mk(st->c_FileSource, 2, st->n_file, file, st->n_name, nm);
            goto done;
        }
    }
    nm = name(p);
    if (nm == NULL) goto done;
    res = mk(st->c_Name, 1, st->n_name, nm);
done:
    Py_XDECREF(file);
    Py_XDECREF(nm);
    Py_XDECREF(sources_t);
    vec_free(&sources);
    return res;
}

static PyObject *
source(Parser *p)
{
    if (stack_exhausted(p)) return too_deep(p);
    return source_(p);
}

/* --- files -------------------------------------------------------------------------- */

/* role or role-term. A subrole such as the 1 in "axiom-1" lexes as the number -1, so
 * the sign is split off that token. */
static int
role(Parser *p, PyObject **role_out, PyObject **sub_out)
{
    const Tok *tok = CUR(p);
    if (tok->kind != K_LOWER) {
        fail_expected(p, "a formula role");
        return -1;
    }
    p->i++;
    Tok *next = CUR(p);
    *sub_out = NULL;
    *role_out = tok_text(p, tok);
    if (*role_out == NULL) return -1;
    if (accept(p, O_MINUS)) {
        *sub_out = general_term(p);
    }
    else if (IS_NUM(next->kind) && p->s[next->start] == '-') {
        next->start += 1;
        *sub_out = general_term(p);
    }
    else {
        *sub_out = Py_NewRef(Py_None);
    }
    if (*sub_out == NULL) {
        Py_CLEAR(*role_out);
        return -1;
    }
    return 0;
}

static PyObject *
annotated_formula(Parser *p, int rule)
{
    State *st = S(p);
    PyObject *nm = NULL, *role_s = NULL, *sub = NULL, *formula = NULL, *source_n = NULL,
             *info = NULL, *annotations = NULL, *line = NULL, *symbols = NULL, *res = NULL;
    const Tok *start = CUR(p);
    p->i++;
    EXPECT(O_LPAREN);
    nm = name(p);
    if (nm == NULL) goto done;
    EXPECT(O_COMMA);
    if (role(p, &role_s, &sub) < 0) goto done;
    EXPECT(O_COMMA);
    begin_formula(p);
    formula = run_rule(p, rule);
    if (formula == NULL) goto done;
    symbols = end_formula(p);
    if (symbols == NULL) goto done;
    if (accept(p, O_COMMA)) {
        source_n = source(p);
        if (source_n == NULL) goto done;
        if (accept(p, O_COMMA)) {
            info = general_list(p);
            if (info == NULL) goto done;
        }
        else {
            info = Py_NewRef(Py_None);
        }
        annotations = mk(st->c_Annotations, 2, st->n_source, source_n, st->n_info, info);
        if (annotations == NULL) goto done;
    }
    else {
        annotations = Py_NewRef(Py_None);
    }
    EXPECT(O_RPAREN);
    EXPECT(O_DOT);
    line = PyLong_FromSsize_t(start->line);
    if (line == NULL) goto done;
    res = mk(st->c_AnnotatedFormula, 8, st->n_language, st->lang_str[rule], st->n_name, nm,
             st->n_role, role_s, st->n_formula, formula, st->n_annotations, annotations,
             st->n_subrole, sub, st->n_line, line, st->n_symbols, symbols);
done:
    Py_XDECREF(symbols);
    Py_XDECREF(nm);
    Py_XDECREF(role_s);
    Py_XDECREF(sub);
    Py_XDECREF(formula);
    Py_XDECREF(source_n);
    Py_XDECREF(info);
    Py_XDECREF(annotations);
    Py_XDECREF(line);
    return res;
}

static PyObject *
include(Parser *p)
{
    State *st = S(p);
    PyObject *file = NULL, *selection = NULL, *space = NULL, *line = NULL, *res = NULL;
    Vec names = {0};
    const Tok *start = CUR(p);
    p->i++;
    EXPECT(O_LPAREN);
    file = atomic_word(p);
    if (file == NULL) goto done;
    selection = Py_NewRef(Py_None);
    space = Py_NewRef(Py_None);
    if (accept(p, O_COMMA)) {
        if (accept(p, O_STAR)) {
            Py_SETREF(selection, Py_NewRef(st->star));
        }
        else {
            EXPECT(O_LBRACK);
            if (vec_push_new(&names, name(p)) < 0) goto done;
            while (accept(p, O_COMMA)) {
                if (vec_push_new(&names, name(p)) < 0) goto done;
            }
            EXPECT(O_RBRACK);
            PyObject *t = vec_tuple(&names);
            Py_SETREF(selection, t);
            if (selection == NULL) goto done;
        }
        if (accept(p, O_COMMA)) {
            PyObject *sp = name(p);
            Py_SETREF(space, sp);
            if (space == NULL) goto done;
        }
    }
    EXPECT(O_RPAREN);
    EXPECT(O_DOT);
    line = PyLong_FromSsize_t(start->line);
    if (line == NULL) goto done;
    res = mk(st->c_Include, 4, st->n_file, file, st->n_selection, selection, st->n_space,
             space, st->n_line, line);
done:
    Py_XDECREF(file);
    Py_XDECREF(selection);
    Py_XDECREF(space);
    Py_XDECREF(line);
    vec_free(&names);
    return res;
}

static int
rule_of(Parser *p, const Tok *t)
{
    if (t->kind != K_LOWER || t->end - t->start != 3) return -1;
    for (int r = 0; r < NRULES; r++) {
        if (memcmp(p->s + t->start, RULE_NAMES[r], 3) == 0) return r;
    }
    return -1;
}

static PyObject *
parse_file_rule(Parser *p)
{
    State *st = S(p);
    PyObject *inputs_t = NULL, *comments_t = NULL, *symbols_t = NULL, *res = NULL;
    Vec inputs = {0};
    while (KIND(p) != K_EOF) {
        const Tok *tok = CUR(p);
        if (tok->kind == K_LOWER && PEEK(p, 1)->kind == O_LPAREN) {
            int rule;
            if (TEXT_IS(p, tok, "include")) {
                if (vec_push_new(&inputs, include(p)) < 0) goto done;
            }
            else if ((rule = rule_of(p, tok)) >= 0) {
                if (vec_push_new(&inputs, annotated_formula(p, rule)) < 0) goto done;
            }
            else {
                fail_expected(p, "an annotated formula or include");
                goto done;
            }
        }
        else {
            fail_expected(p, "an annotated formula or include");
            goto done;
        }
        if (PyErr_CheckSignals() < 0) goto done;
    }
    inputs_t = vec_tuple(&inputs);
    comments_t = vec_tuple(&p->comments);
    symbols_t = PyTuple_New(p->symtab.n);
    if (inputs_t == NULL || comments_t == NULL || symbols_t == NULL) goto done;
    for (Py_ssize_t k = 0; k < p->symtab.n; k++) {
        PyTuple_SET_ITEM(symbols_t, k, Py_NewRef(p->symtab.ent[k].obj));
    }
    res = mk(st->c_TPTPFile, 4, st->n_inputs, inputs_t, st->n_comments, comments_t,
             st->n_filename, p->filename, st->n_symbols, symbols_t);
done:
    Py_XDECREF(inputs_t);
    Py_XDECREF(comments_t);
    Py_XDECREF(symbols_t);
    vec_free(&inputs);
    return res;
}

/* ------------------------------------------------------------------------------------
 * Python interface
 * ---------------------------------------------------------------------------------- */

static void
parser_free(Parser *p)
{
    PyMem_Free(p->toks);
    vec_free(&p->comments);
    ev_clear(p);
    PyMem_Free(p->events);
    PyMem_Free(p->undo);
    htab_free(&p->vars);
    htab_free(&p->symtab);
}

/* Shared driver. rule < 0 parses a whole file; otherwise a single bare formula. */
static PyObject *
run_parser(State *st, PyObject *text, PyObject *filename, int rule)
{
    Parser p;
    memset(&p, 0, sizeof p);
    p.st = st;
    p.text = text;
    p.filename = filename;
    p.stack_base = (uintptr_t)&p;
    p.s = PyUnicode_AsUTF8AndSize(text, &p.n);
    if (p.s == NULL) {
        if (PyErr_ExceptionMatches(PyExc_UnicodeEncodeError)) {
            PyErr_Clear(); /* lone surrogates: let the Python parser deal with it */
            Py_RETURN_NOTIMPLEMENTED;
        }
        return NULL;
    }

    PyObject *res = NULL;
    /* The trees built here have no reference cycles, and the garbage collector would walk the
     * growing tree again and again, which costs about a third of the time. The GIL is held
     * throughout, so nothing else runs while it is off. */
    int gc_was_enabled = PyGC_Disable();
    if (lex(&p) < 0) goto done;
    if (rule < 0) {
        res = parse_file_rule(&p);
    }
    else {
        begin_formula(&p);
        res = run_rule(&p, rule);
        if (res != NULL && KIND(&p) != K_EOF) {
            Py_CLEAR(res);
            fail_desc(&p, CUR(&p), CUR(&p), "unexpected %U after the formula");
        }
    }
done:
    if (gc_was_enabled) PyGC_Enable();
    if (res == NULL && p.too_deep) {
        PyErr_Clear();
        res = Py_NewRef(Py_NotImplemented);
    }
    parser_free(&p);
    return res;
}

static PyObject *
cparser_parse(PyObject *module, PyObject *args)
{
    PyObject *text, *filename;
    if (!PyArg_ParseTuple(args, "UO:parse", &text, &filename)) return NULL;
    return run_parser((State *)PyModule_GetState(module), text, filename, -1);
}

static PyObject *
cparser_parse_formula(PyObject *module, PyObject *args)
{
    PyObject *text, *language;
    if (!PyArg_ParseTuple(args, "UU:parse_formula", &text, &language)) return NULL;
    int rule = -1;
    for (int r = 0; r < NRULES; r++) {
        PyObject *name_r = PyUnicode_FromString(RULE_NAMES[r]);
        if (name_r == NULL) return NULL;
        int eq = PyUnicode_Compare(language, name_r) == 0;
        Py_DECREF(name_r);
        if (eq) {
            rule = r;
            break;
        }
    }
    if (rule < 0) {
        PyErr_Format(PyExc_ValueError, "unknown TPTP language %R", language);
        return NULL;
    }
    return run_parser((State *)PyModule_GetState(module), text, Py_None, rule);
}

static PyMethodDef cparser_methods[] = {
    {"parse", cparser_parse, METH_VARARGS,
     "parse(text, filename) -> TPTPFile, or NotImplemented if the Python parser must be used."},
    {"parse_formula", cparser_parse_formula, METH_VARARGS,
     "parse_formula(text, language) -> Node, or NotImplemented if the Python parser must be used."},
    {NULL, NULL, 0, NULL},
};

static int
cparser_exec(PyObject *module)
{
    State *st = (State *)PyModule_GetState(module);
    PyObject *nodes = PyImport_ImportModule("thousandproblems.nodes");
    if (nodes == NULL) return -1;

    st->error_at = PyObject_GetAttrString(nodes, "_error_at");
    if (st->error_at == NULL) goto fail;
    st->star = PyUnicode_InternFromString("*");
    if (st->star == NULL) goto fail;
#define X(n)                                                                               \
    st->c_##n = PyObject_GetAttrString(nodes, #n);                                         \
    if (st->c_##n == NULL) goto fail;
    CLASS_LIST(X)
#undef X
#define X(n)                                                                               \
    st->n_##n = PyUnicode_InternFromString(#n);                                            \
    if (st->n_##n == NULL) goto fail;
    FIELD_LIST(X)
#undef X

    {
        const OpDef *tables[3] = {OPS3, OPS2, OPS1};
        int counts[3] = {COUNT(OPS3), COUNT(OPS2), COUNT(OPS1)};
        for (int t = 0; t < 3; t++) {
            for (int k = 0; k < counts[t]; k++) {
                st->opstr[tables[t][k].kind] = PyUnicode_InternFromString(tables[t][k].text);
                if (st->opstr[tables[t][k].kind] == NULL) goto fail;
            }
        }
    }
    for (int r = 0; r < NRULES; r++) {
        st->lang_str[r] = PyUnicode_InternFromString(RULE_NAMES[r]);
        if (st->lang_str[r] == NULL) goto fail;
    }

    st->cls_of_op[O_NOT] = st->c_Not;
    st->cls_of_op[O_BOX] = st->c_Box;
    st->cls_of_op[O_DIAMOND] = st->c_Diamond;
    st->cls_of_op[O_BRACE_DOT] = st->c_Unary;
    st->cls_of_op[O_PAREN_DOT] = st->c_Unary;
    st->cls_of_op[O_OR] = st->c_Or;
    st->cls_of_op[O_AND] = st->c_And;
    st->cls_of_op[O_AT] = st->c_Apply;
    st->cls_of_op[O_STAR] = st->c_Product;
    st->cls_of_op[O_PLUS] = st->c_Sum;
    st->cls_of_op[O_IMPLIES] = st->c_Implies;
    st->cls_of_op[O_IMPLIED_BY] = st->c_ImpliedBy;
    st->cls_of_op[O_IFF] = st->c_Iff;
    st->cls_of_op[O_XOR] = st->c_Xor;
    st->cls_of_op[O_NOR] = st->c_Nor;
    st->cls_of_op[O_NAND] = st->c_Nand;
    st->cls_of_op[O_EQ] = st->c_Equals;
    st->cls_of_op[O_NEQ] = st->c_NotEquals;
    st->cls_of_op[O_GT] = st->c_Arrow;
    st->cls_of_op[O_DEFEQ] = st->c_Definition;
    st->cls_of_op[O_ASSIGN] = st->c_Assignment;
    st->cls_of_op[O_SEQUENT] = st->c_Sequent;
    st->cls_of_op[O_SUBTYPE] = st->c_Subtype;
    st->cls_of_op[O_COLON] = st->c_Colon;
    st->cls_of_op[O_FORALL] = st->c_ForAll;
    st->cls_of_op[O_EXISTS] = st->c_Exists;
    st->cls_of_op[O_CARET] = st->c_Lambda;
    st->cls_of_op[O_CHOICE] = st->c_Choice;
    st->cls_of_op[O_DESCRIPTION] = st->c_Description;
    st->cls_of_op[O_TYPE_FORALL] = st->c_TypeForAll;
    st->cls_of_op[O_TYPE_EXISTS] = st->c_TypeExists;
    st->cls_of_op[O_HASH] = st->c_Quantified;

    Py_DECREF(nodes);
    return 0;
fail:
    Py_DECREF(nodes);
    return -1;
}

static PyModuleDef_Slot cparser_slots[] = {
    {Py_mod_exec, cparser_exec},
#ifdef Py_mod_multiple_interpreters
    {Py_mod_multiple_interpreters, Py_MOD_PER_INTERPRETER_GIL_SUPPORTED},
#endif
    {0, NULL},
};

static struct PyModuleDef cparser_module = {
    PyModuleDef_HEAD_INIT,
    "thousandproblems._cparser",
    "C implementation of the TPTP lexer and parser. Use thousandproblems.parse, not this.",
    sizeof(State),
    cparser_methods,
    cparser_slots,
    state_traverse,
    state_clear,
    state_free,
};

PyMODINIT_FUNC
PyInit__cparser(void)
{
    for (int t = 0; t < 3; t++) {
        const OpDef *tab = t == 0 ? OPS3 : t == 1 ? OPS2 : OPS1;
        int cnt = t == 0 ? COUNT(OPS3) : t == 1 ? COUNT(OPS2) : COUNT(OPS1);
        for (int k = 0; k < cnt; k++) OP_TEXT[tab[k].kind] = tab[k].text;
    }
    memset(ONE_CHAR, -1, sizeof ONE_CHAR);
    for (int k = 0; k < COUNT(OPS1); k++) {
        ONE_CHAR[(unsigned char)OPS1[k].text[0]] = (signed char)OPS1[k].kind;
    }
    return PyModuleDef_Init(&cparser_module);
}
